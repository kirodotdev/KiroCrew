"""Tests for sub-agent turn resilience (subagent.py).

Covers the three guard-parity fixes that bring sub-agents in line with the
main agent's turn-resilience ladder:

1. Transient-backend retry inside ``_run_inner`` (``_stream_with_transient_retry``):
   pre-token re-prompt / post-token CONTINUE, budget-capped, ``subagent_retrying``
   UI event. Mirrors chat_runner B1/B2 (PR #91).
2. User-stop semantics (``cancel``): neutral terminal state — partial output
   preserved, ``user_stop`` tombstone, ``subagent_done`` carries ``stopped: true``.
3. Unexpected-cancel one-shot auto-continue (``_schedule_cancel_recovery``):
   a non-user, non-shutdown task cancellation respawns the run exactly once.
   Mirrors the main path's cancel recovery (PR #173).
4. Orphan-notification wiring: ``_try_inject_orphan_notification`` /
   ``_send_orphan_slack_dm`` delegate to the gateway-wired callbacks instead of
   being stubs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.llm_helpers import TRANSIENT_RETRIES
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.subagent import (
    _TRANSIENT_CONTINUE_MSG,
    SubagentInfo,
    SubagentManager,
)

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py``.

# Two kinds of number live here and they must not be merged.
#
# POSITIVE WAITS (_START_TIMEOUT, _RESPAWN_TIMEOUT) bound how long the harness waits
# for the event loop to schedule something. Every ordering guarantee around them is
# asserted separately, and the fixed ``asyncio.sleep`` windows that prove a respawn
# has NOT fired yet are deliberately left alone -- those are negative assertions,
# where the duration IS the test. Raising a positive wait cannot weaken an assertion;
# it only stops the harness giving up before the awaited thing was ever given a
# chance to run. 5.0s was too tight on a loaded runner: shard 4 of the Windows
# backend job runs ~950s wall with individual tests over 38s, and
# ``test_cancel_recovery_waits_for_slow_teardown`` timed out there while the other
# 17,258 tests in the same shard passed.
#
# _RESPAWN_TIMEOUT stays BELOW the production give-up it can outlive on the FAILURE
# path (``subagent._RECOVERY_SLOT_WAIT_SECS`` = 60.0). A passing run never reaches
# that give-up -- ``test_cancel_recovery_waits_for_free_slot`` frees the slot while
# the poll is still young, and every other caller finds capacity already free, so
# the bounded wait exits on its next tick. The bound is about what a FAILING run
# reports: if the poll outlived 60.0s, the code would have already raised "no free
# slot for recovery respawn", ``task2`` would never appear, and the failure would
# read as "recovery never happened" rather than naming the real cause.
#
# Ceiling for both: ``setup.cfg`` sets a global ``--timeout=120``. The heaviest test
# serializes two start waits plus one respawn poll, so the failure path must stay
# under that or a real hang surfaces as an opaque pytest-timeout kill instead of the
# named deadline that explains it.
_START_TIMEOUT = 30.0  # the mocked stream reaching its first yield
_RESPAWN_TIMEOUT = 20.0  # a cancelled run's replacement task appearing in _tasks

# An UPPER BOUND, not a positive wait -- do not raise it with the two above.
# ``test_cancel_recovery_failure_emits_done_and_delivers`` patches the production
# give-up (``subagent._RECOVERY_SLOT_WAIT_SECS``, normally 60.0) down to 0.4s, and
# this bound is what asserts the patch actually took effect: the recovery must fail
# FAST. Widening it opens a band in which a refactor that stops reading that module
# global -- inlining the literal, moving it onto the instance, renaming it -- leaves
# every terminal assertion still passing, for the wrong reason.
_GIVE_UP_BOUND = 10.0


class _TransientError(Exception):
    """Duck-typed AcpError carrying the structured transient verdict."""

    transient = True


class _FatalError(Exception):
    transient = False


def _text_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text, runtime_global=False)


def _complete_event() -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)


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
    # Force the dedicated-process path (deterministic under MagicMock sessions).
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    return mgr


async def _spawn_and_wait(mgr: SubagentManager, task: str = "do work") -> SubagentInfo:
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(task)
        assert info is not None
        await mgr._tasks[info.id]
    return info


# ── 1. Transient-backend retry ───────────────────────────────────────


@pytest.mark.asyncio
async def test_transient_error_pretoken_retries_same_prompt():
    """A transient error before any token re-sends the SAME prompt and succeeds."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                raise _TransientError("backend 500")
            yield _text_event("recovered result")
            yield _complete_event()

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    events: list[tuple[str, dict]] = []
    _orig_fire = mgr._fire_event

    async def _spy(etype, info, extra=None):
        events.append((etype, extra or {}))
        await _orig_fire(etype, info, extra)

    mgr._fire_event = _spy

    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.error == ""
    assert "recovered result" in info.result
    assert calls == ["built_message", "built_message"]  # pre-token: same prompt
    assert any(e[0] == "subagent_retrying" for e in events)


@pytest.mark.asyncio
async def test_transient_error_posttoken_sends_continue_prompt():
    """A transient error AFTER tokens streamed sends the CONTINUE prompt."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                yield _text_event("partial ")
                raise _TransientError("mid-stream 500")
            yield _text_event("finished")
            yield _complete_event()

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.error == ""
    assert "partial" in info.result and "finished" in info.result
    assert calls[0] == "built_message"
    assert calls[1] == _TRANSIENT_CONTINUE_MSG  # post-token: continue, not re-run


@pytest.mark.asyncio
async def test_transient_budget_exhausted_propagates():
    """Persistent transient errors fail after TRANSIENT_RETRIES attempts."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise _TransientError("backend 500")
            yield  # noqa: unreachable — async generator marker

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with (
        patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0),
        # fallback_model="" (disabled): this test pins the PRE-FEATURE budget
        # behavior — the default is now "auto", which would walk the chain.
        patch("kiro_crew.subagent.configured_fallback_chain", return_value=()),
    ):
        info = await _spawn_and_wait(mgr)

    assert info.done is True
    assert "500" in info.error
    assert len(calls) == 1 + TRANSIENT_RETRIES  # initial + retries


@pytest.mark.asyncio
async def test_throttle_fallback_chain_swaps_model_and_annotates():
    """Zero-activity budget exhaustion walks agent.fallback_model: the
    substitute set_model moves the session onto the candidate, the original
    prompt is replayed, and the delivered result carries the visible
    fallback warning (never silent)."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) <= 1 + TRANSIENT_RETRIES:
                raise _TransientError("backend throttle")
            yield _text_event("fb result")
            yield _complete_event()

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = sessions._provider
    provider.available_models = MagicMock(return_value=[{"modelId": "fb-1"}])
    provider.served_model = "primary-model"
    provider._model = "primary-model"

    # Successful set_model syncs the model attrs (real-provider behavior);
    # the walk witness reads this to confirm the swap landed.
    async def _move(model_id):
        provider._model = model_id
        provider.served_model = model_id

    provider.set_model = AsyncMock(side_effect=_move)

    mgr = _manager(sessions)
    with (
        patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0),
        patch("kiro_crew.subagent.configured_fallback_chain", return_value=("fb-1",)),
    ):
        info = await _spawn_and_wait(mgr)

    assert info.error == ""
    provider.set_model.assert_awaited_once_with("fb-1")
    # Zero activity by construction — the ORIGINAL prompt is replayed.
    assert calls == ["built_message"] * (2 + TRANSIENT_RETRIES)
    # Visibility: the delivered result is prefixed with the fallback warning.
    assert "fb result" in info.result
    assert "throttled" in info.result and "fb-1" in info.result


@pytest.mark.asyncio
async def test_throttle_fallback_chain_exhausted_propagates():
    """Every candidate also fails: the error surfaces after the bounded
    per-candidate attempts, exactly like today's exhaustion."""
    from kiro_crew.llm_helpers import FALLBACK_CANDIDATE_ATTEMPTS

    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise _TransientError("backend throttle 500")
            yield  # noqa: unreachable — async generator marker

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = sessions._provider
    provider.available_models = MagicMock(return_value=[{"modelId": "fb-1"}])
    provider.served_model = "primary-model"
    provider._model = "primary-model"

    # Successful set_model syncs the model attrs (real-provider behavior);
    # the walk witness reads this to confirm the swap landed.
    async def _move(model_id):
        provider._model = model_id
        provider.served_model = model_id

    provider.set_model = AsyncMock(side_effect=_move)

    mgr = _manager(sessions)
    with (
        patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0),
        patch("kiro_crew.subagent.configured_fallback_chain", return_value=("fb-1",)),
    ):
        info = await _spawn_and_wait(mgr)

    assert info.done is True
    assert "500" in info.error
    # #5447 item 1: the terminal error text names the WHOLE walk, not just the
    # last candidate's failure — the chain story is appended to info.error.
    assert "primary-model throttled" in info.error
    assert "fb-1" in info.error and "also unavailable" in info.error
    assert len(calls) == 1 + TRANSIENT_RETRIES + FALLBACK_CANDIDATE_ATTEMPTS
    provider.set_model.assert_awaited_once_with("fb-1")


@pytest.mark.asyncio
async def test_throttle_fallback_ladder_routes_through_shared_budget_body():
    """DRIFT PIN (#5447 item 2): the ladder must consult
    FallbackState.should_retry_active for the per-candidate budget. Forcing
    the shared body to refuse retries changes the attempt count — proof the
    budget is not re-encoded locally (mirror of the stream_and_collect pin in
    test_llm_helpers.py)."""
    from kiro_crew.llm_helpers import FallbackState

    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise _TransientError("backend throttle 500")
            yield  # noqa: unreachable — async generator marker

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = sessions._provider
    provider.available_models = MagicMock(return_value=[{"modelId": "fb-1"}])
    provider.served_model = "primary-model"
    provider._model = "primary-model"

    async def _move(model_id):
        provider._model = model_id
        provider.served_model = model_id

    provider.set_model = AsyncMock(side_effect=_move)

    mgr = _manager(sessions)
    with (
        patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0),
        patch("kiro_crew.subagent.configured_fallback_chain", return_value=("fb-1",)),
        patch.object(FallbackState, "should_retry_active", return_value=False),
    ):
        info = await _spawn_and_wait(mgr)

    assert info.done is True
    # Budget refused ⇒ the candidate gets only its single post-advance attempt.
    assert len(calls) == 1 + TRANSIENT_RETRIES + 1


@pytest.mark.asyncio
async def test_throttle_fallback_story_survives_a_verbose_error():
    """A verbose backend error fills _describe_exception to its cap — the
    story must still be present in info.error (the error tail is what gets
    trimmed, never the walk), and the total stays bounded."""
    from kiro_crew.subagent import _MAX_ERROR_DETAIL_LEN

    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise _TransientError("backend throttle 500 " + "x" * (3 * _MAX_ERROR_DETAIL_LEN))
            yield  # noqa: unreachable — async generator marker

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = sessions._provider
    provider.available_models = MagicMock(return_value=[{"modelId": "fb-1"}])
    provider.served_model = "primary-model"
    provider._model = "primary-model"

    async def _move(model_id):
        provider._model = model_id
        provider.served_model = model_id

    provider.set_model = AsyncMock(side_effect=_move)

    mgr = _manager(sessions)
    with (
        patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0),
        patch("kiro_crew.subagent.configured_fallback_chain", return_value=("fb-1",)),
    ):
        info = await _spawn_and_wait(mgr)

    assert info.done is True
    assert len(info.error) <= _MAX_ERROR_DETAIL_LEN
    assert info.error.endswith("[primary-model throttled; fallbacks fb-1 also unavailable]")


@pytest.mark.asyncio
async def test_non_transient_error_fails_immediately():
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise _FatalError("auth denied")
            yield  # noqa: unreachable

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    info = await _spawn_and_wait(mgr)

    assert info.done is True
    assert "auth denied" in info.error
    assert len(calls) == 1  # no retry


# ── 2. User-stop semantics ───────────────────────────────────────────


def _hanging_stream_factory(started: asyncio.Event):
    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            yield _text_event("partial work ")
            started.set()
            await asyncio.Event().wait()  # hang until cancelled
            yield _complete_event()

        return _gen()

    return stream_factory


@pytest.mark.asyncio
async def test_user_cancel_is_neutral_stopped_with_partial():
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))
    events: list[tuple[str, dict]] = []

    async def _spy(etype, info, extra=None):
        events.append((etype, extra or {}))

    mgr._fire_event = _spy

    with patch("kiro_crew.subagent.Stats") as stats, patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("long job")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        cancelled = await mgr.cancel(info.id)
        await asyncio.gather(*mgr._tasks.values(), return_exceptions=True)

    assert cancelled is True
    assert info.user_stopped is True
    assert info.done is True
    # Neutral semantics live in the RECORD: no error for a user stop, so
    # reconnect snapshots / tombstones / API listings all derive "stopped".
    assert not info.error
    assert "partial work" in info.result  # streamed partial preserved
    done_events = [e for e in events if e[0] == "subagent_done"]
    # Exactly ONE terminal event (from _force_reap) — cancel() must not
    # emit a duplicate. It is stopped-aware and error-free.
    assert len(done_events) == 1
    assert done_events[-1][1].get("stopped") is True
    assert done_events[-1][1].get("error") is None
    # Neutral outcome: user stop is not counted as a failure.
    stats.return_value.inc_subagent_failed.assert_not_called()


# ── 3. Unexpected-cancel one-shot auto-continue ──────────────────────


@pytest.mark.asyncio
async def test_unexpected_cancel_auto_continues_once():
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("interruptible job")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        task1 = mgr._tasks[info.id]
        task1.cancel()  # UNEXPECTED cancel (not via mgr.cancel, not shutdown)
        await asyncio.gather(task1, return_exceptions=True)

        # One-shot recovery: not terminal, retry budget consumed.
        assert info.done is False
        assert info._cancel_retry_used is True

        # Recovery respawns on a fresh task AFTER the original task's
        # teardown fully completes (explicit handshake, not a timed sleep).
        started.clear()
        task2 = None
        deadline = asyncio.get_event_loop().time() + _RESPAWN_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            task2 = mgr._tasks.get(info.id)
            if task2 is not None and task2 is not task1:
                break
            await asyncio.sleep(0.05)
        assert task2 is not None and task2 is not task1
        assert task1.done()  # respawn never races the original teardown

        # Second unexpected cancel → terminal (budget spent).
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)

    assert info.done is True
    assert info.error == "cancelled"


@pytest.mark.asyncio
async def test_unexpected_cancel_after_tool_activity_finalizes_without_respawn():
    """Once ANY tool has executed, an unexpected cancel must NOT auto-respawn:
    the respawn would run on a fresh session with no tool ledger, so the model
    cannot verify which side effects already happened — a preamble alone
    cannot make re-running safe (Arbiter item 1 / Design finding 1). The run
    is finalized with an explicit suppression error instead."""
    from kiro_crew.acp.client import EVENT_TOOL_CALL

    started = asyncio.Event()
    stream_calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        stream_calls.append(msg)

        async def _gen():
            yield SimpleNamespace(
                kind=EVENT_TOOL_CALL,
                title="Running: write_file",
                tool_kind="edit",
                tool_call_id="tc1",
                tool_input={},
                runtime_global=False,
            )
            started.set()
            await asyncio.Event().wait()  # hang until cancelled — NO text ever
            yield _complete_event()

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("side-effecting job")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        assert info.tool_count > 0 and info.streaming_text == ""
        task1 = mgr._tasks[info.id]
        task1.cancel()  # UNEXPECTED cancel after tool ran
        await asyncio.gather(task1, return_exceptions=True)

        # Terminal immediately — no recovery scheduled, no respawn task.
        assert info.done is True
        assert info._recovering is False
        assert "auto-continue suppressed" in info.error
        assert info.outcome == "failed"
        assert mgr._tasks.get(f"{info.id}:recovery") is None
        # Give the loop a beat: no second stream call may ever appear.
        await asyncio.sleep(0.2)
        assert len(stream_calls) == 1


@pytest.mark.asyncio
async def test_cancel_recovery_text_only_respawn_gets_resume_preamble():
    """Text-only activity is safe to resume: the respawned prompt must carry
    the interruption preamble so the model continues instead of restarting."""
    from kiro_crew.subagent import _CANCEL_RESUME_PREFIX

    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))
    build_msgs: list[str] = []
    orig_build = mgr._ctx_builder.build_message

    def _capture(msg, *a, **kw):
        build_msgs.append(msg)
        return orig_build(msg, *a, **kw)

    mgr._ctx_builder.build_message = MagicMock(side_effect=_capture)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("resumable job")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        assert info.streaming_text and info.tool_count == 0
        task1 = mgr._tasks[info.id]
        task1.cancel()  # UNEXPECTED cancel after text, no tools
        await asyncio.gather(task1, return_exceptions=True)

        # Wait for the respawn's build_message call (second entry).
        deadline = asyncio.get_event_loop().time() + _RESPAWN_TIMEOUT
        while len(build_msgs) < 2 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert len(build_msgs) >= 2

        # First attempt: bare prompt. Respawn: preamble present.
        assert not build_msgs[0].startswith(_CANCEL_RESUME_PREFIX)
        assert build_msgs[1].startswith(_CANCEL_RESUME_PREFIX)

        # Cleanup: terminate the respawned run.
        task2 = mgr._tasks.get(info.id)
        if task2 is not None:
            task2.cancel()
            await asyncio.gather(task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancel_does_not_auto_continue():
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("job at shutdown")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        await mgr.cancel_all()

    assert mgr._shutting_down is True
    assert info._cancel_retry_used is False  # no recovery attempted
    assert info.done is True


# ── 4. Orphan-notification wiring ────────────────────────────────────


@pytest.mark.asyncio
async def test_orphan_injection_delegates_to_callback():
    notify = AsyncMock(return_value=True)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)
    with patch("kiro_crew.subagent.sel"):
        ok = await mgr._try_inject_orphan_notification("dashboard:main", "msg")
    assert ok is True
    # The structured completion facts (#1792) are forwarded as a third arg;
    # a direct call with no meta passes None through unchanged.
    notify.assert_awaited_once_with("dashboard:main", "msg", None)


@pytest.mark.asyncio
async def test_orphan_injection_false_without_callback():
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None)
    assert await mgr._try_inject_orphan_notification("dashboard:main", "msg") is False


@pytest.mark.asyncio
async def test_orphan_injection_callback_error_returns_false():
    notify = AsyncMock(side_effect=RuntimeError("dashboard unavailable"))
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)

    assert await mgr._try_inject_orphan_notification("dashboard:main", "msg") is False
    notify.assert_awaited_once_with("dashboard:main", "msg", None)


@pytest.mark.asyncio
async def test_delivered_orphan_survives_audit_and_tombstone_failures():
    notify = AsyncMock(return_value=True)
    audit = MagicMock()
    audit.log_api_access.side_effect = RuntimeError("audit unavailable")
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_notify=notify)
    state = {
        "id": "orphan-1",
        "task": "recover work",
        "parent_session": "dashboard:main",
    }

    with (
        patch("kiro_crew.subagent.has_dashboard_surface", return_value=True),
        patch("kiro_crew.subagent.sel", return_value=audit),
        patch(
            "kiro_crew.subagent.write_tombstone", side_effect=OSError("disk unavailable")
        ) as write_tombstone,
    ):
        result = await mgr._notify_orphan("orphan-1", state, "notification_pending", False)

    assert result is None
    notify.assert_awaited_once()
    audit.log_api_access.assert_called_once_with(
        caller="dashboard:main",
        operation="subagent.orphan_notification_injected",
        outcome="ok",
        source="subagent",
    )
    write_tombstone.assert_called_once_with(
        "orphan-1",
        cause="gateway_restart",
        recovery_action="delivered",
        pid=None,
        turns=0,
        last_tool="",
    )


@pytest.mark.asyncio
async def test_orphan_dm_delegates_to_callback():
    dm = AsyncMock(return_value=True)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_dm=dm)
    await mgr._send_orphan_slack_dm("orphan msg")
    dm.assert_awaited_once_with("orphan msg")


@pytest.mark.asyncio
async def test_orphan_dm_callback_error_is_swallowed():
    dm = AsyncMock(side_effect=RuntimeError("slack down"))
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_dm=dm)
    await mgr._send_orphan_slack_dm("orphan msg")  # must not raise


@pytest.mark.asyncio
async def test_cancel_recovery_waits_for_slow_teardown():
    """Respawn must not race a slow session reset in the original finally.

    The original run's finally awaits ``sessions.reset`` (up to 30s in prod).
    The recovery handshake is explicit: the respawn only happens after the
    original task object has fully completed, so a 0.5s-slow reset must delay
    the respawn past it — never fire mid-teardown.
    """
    started = asyncio.Event()
    sessions = _mock_sessions(_hanging_stream_factory(started))

    reset_done = asyncio.Event()

    async def _slow_reset(key):
        await asyncio.sleep(0.5)
        reset_done.set()

    sessions.reset = AsyncMock(side_effect=_slow_reset)
    mgr = _manager(sessions)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("slow teardown job")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        task1 = mgr._tasks[info.id]
        task1.cancel()
        await asyncio.gather(task1, return_exceptions=True)

        # Poll for the respawn; when it appears, teardown MUST already be done.
        task2 = None
        deadline = asyncio.get_event_loop().time() + _RESPAWN_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            task2 = mgr._tasks.get(info.id)
            if task2 is not None and task2 is not task1:
                break
            await asyncio.sleep(0.05)
        assert task2 is not None and task2 is not task1
        assert reset_done.is_set()  # respawn strictly after the slow reset
        assert task1.done()
        # The new task is tracked — a user Stop can still find and cancel it.
        assert mgr._tasks.get(info.id) is task2
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_recovery_waits_for_free_slot():
    """The respawn re-acquires capacity — never exceeds max_concurrent."""
    started = asyncio.Event()
    mgr = _manager(_mock_sessions(_hanging_stream_factory(started)))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("capacity job")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        task1 = mgr._tasks[info.id]

        # Simulate the freed slot being immediately taken by a queued spawn.
        mgr._max_concurrent = 1

        async def _occupy_after_teardown():
            await asyncio.gather(task1, return_exceptions=True)
            mgr._running_count = 1  # drained spawn holds the only slot

        occupier = asyncio.create_task(_occupy_after_teardown())
        task1.cancel()
        await occupier

        # Recovery must WAIT while the pool is full.
        await asyncio.sleep(0.6)
        assert mgr._running_count <= mgr._max_concurrent
        assert mgr._tasks.get(info.id) in (None, task1)

        # Free the slot — recovery proceeds and count never exceeds the cap.
        mgr._running_count = 0
        task2 = None
        deadline = asyncio.get_event_loop().time() + _RESPAWN_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            task2 = mgr._tasks.get(info.id)
            if task2 is not None and task2 is not task1:
                break
            await asyncio.sleep(0.05)
        assert task2 is not None and task2 is not task1
        assert mgr._running_count <= mgr._max_concurrent
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_recovery_failure_emits_done_and_delivers():
    """A failed recovery still finalizes: subagent_done fires and _on_done runs.

    If the respawn can't happen (no capacity within the deadline), the UI must
    not be left on a running card and the parent must still be notified.
    """
    started = asyncio.Event()
    on_done = AsyncMock()
    sessions = _mock_sessions(_hanging_stream_factory(started))
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder(), on_done=on_done)
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    events: list[tuple[str, dict]] = []

    async def _spy(etype, info, extra=None):
        events.append((etype, extra or {}))

    mgr._fire_event = _spy

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent._RECOVERY_SLOT_WAIT_SECS", 0.4),
    ):
        info = mgr.spawn("doomed recovery job")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        task1 = mgr._tasks[info.id]
        task1.cancel()
        await asyncio.gather(task1, return_exceptions=True)

        # Keep the pool permanently full so the slot wait times out.
        mgr._max_concurrent = 1
        mgr._running_count = 1

        # Deterministic: await the registered pending-recovery task itself.
        # Polling info.done races CI schedulers — done flips before the
        # failure path's subagent_done emit and on_done delivery run.
        rec = mgr._tasks.get(f"{info.id}:recovery")
        assert rec is not None, "pending recovery must be registered in _tasks"
        await asyncio.wait_for(asyncio.gather(rec, return_exceptions=True), timeout=_GIVE_UP_BOUND)

    assert info.done is True
    assert info.error == "cancelled (recovery failed)"
    done_events = [e for e in events if e[0] == "subagent_done"]
    assert done_events, "recovery failure must emit subagent_done"
    assert done_events[-1][1].get("error")
    on_done.assert_awaited_once_with(info)


@pytest.mark.asyncio
async def test_cancel_all_reaches_pending_recovery_and_finalizes():
    """A pending cancel-recovery is registered in _tasks so cancel_all cancels
    it, and the cancelled recovery finalizes the record terminally (never
    respawns, never leaves _recovering limbo). Locks in the arbiter's
    shutdown-reachability requirement for the recovery branch."""
    started = asyncio.Event()
    on_done = AsyncMock()
    sessions = _mock_sessions(_hanging_stream_factory(started))
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder(), on_done=on_done)
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._fire_event = AsyncMock()

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        info = mgr.spawn("job interrupted by shutdown")
        assert info is not None
        await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT)
        task1 = mgr._tasks[info.id]
        task1.cancel()
        await asyncio.gather(task1, return_exceptions=True)

        # Recovery is pending and MUST be reachable by cancel_all.
        rec = mgr._tasks.get(f"{info.id}:recovery")
        assert rec is not None, "pending recovery must be registered in _tasks"

        await mgr.cancel_all()

    assert rec.done()
    assert info._recovering is False
    assert info.done is True
    assert info.error == "cancelled"
    # The respawned run never started: the original task was the only one.
    assert mgr._tasks == {}


@pytest.mark.asyncio
async def test_transient_error_after_tool_call_sends_continue_prompt():
    """A transient error after TOOL activity (no text yet) must send CONTINUE —
    replaying the full prompt could re-execute the mutating tool that already
    ran (duplicate writes/messages)."""
    from kiro_crew.acp.client import EVENT_TOOL_CALL

    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                yield SimpleNamespace(
                    kind=EVENT_TOOL_CALL,
                    title="Running: write_file",
                    tool_kind="edit",
                    tool_call_id="tc1",
                    tool_input={},
                    runtime_global=False,
                )
                raise _TransientError("500 before first token")
            yield _text_event("done after tool")
            yield _complete_event()

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.error == ""
    assert "done after tool" in info.result
    assert calls[0] == "built_message"
    # Tool activity counts as post-activity: CONTINUE, never a full replay.
    assert calls[1] == _TRANSIENT_CONTINUE_MSG


@pytest.mark.asyncio
async def test_post_activity_retry_is_one_shot():
    """After ANY observed activity, transient recovery gets exactly ONE
    continuation turn — a second post-activity transient error must propagate,
    matching the main path's ``_posttoken_retry_used`` rule (each continuation
    after a mutating tool is an independent chance to repeat side effects)."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            yield _text_event("some output ")
            raise _TransientError("500 mid-stream")

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    # Attempt 1 (original) → activity → ONE continuation → second post-activity
    # error propagates. Never a third turn, even with TRANSIENT_RETRIES > 1.
    assert TRANSIENT_RETRIES > 1  # the rule must be stricter than the budget
    assert len(calls) == 2
    assert calls[1] == _TRANSIENT_CONTINUE_MSG
    assert info.error != ""
    assert info.outcome == "failed"


def test_no_raw_cancel_outside_chokepoint():
    """Source scan: every raw ``.cancel()`` on a managed run task in
    subagent.py must route through ``_cancel_task_intentionally`` (the
    mechanical enforcement of the intentional-cancel marker contract).
    Allowed raw sites: the chokepoint body itself and observer or recovery
    tasks — none of the latter are managed runs, so the marker contract (and
    recovery) never applies to them."""
    import inspect
    from pathlib import Path

    import kiro_crew.subagent as subagent_mod

    source_root = Path(subagent_mod.__file__).resolve().parent
    source_paths = [Path(subagent_mod.__file__).resolve()]
    source_paths.extend(sorted((source_root / "subagent_manager").glob("*.py")))
    raw_sites = [
        (path.relative_to(source_root).as_posix(), i + 1, line.strip())
        for path in source_paths
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
        if ".cancel()" in line
        and not line.strip().startswith("#")
        and "``" not in line  # docstring mentions, not call sites
    ]
    allowed_substrings = (
        "task.cancel()",  # chokepoint body — verified below to be unique
        "self._reaper_task.cancel()",
        "reconcile_task.cancel()",
        # A reap supersedes a pending respawn; the recovery task schedules the
        # respawn and is NOT a managed run, so no terminal marker applies.
        "recovery_task.cancel()",
        # Shielded terminal-report tasks drained at shutdown — also not managed
        # runs; cancelling them cannot trigger a respawn.
        "report_task.cancel()",
        # Coordinator reconciliation and lease-heartbeat tasks are observers,
        # not managed runs. Cancelling them cannot trigger run recovery.
        "reconcile_task.cancel()",
        "lease_task.cancel()",
        # follow_up watchers (spawn_steer mode="follow_up") — observers, not
        # managed runs: no terminal marker applies, and cancelling one cannot
        # trigger a respawn (it only ever DISPATCHES via continue_conversation,
        # which cancel_all pre-empts by cancelling watchers first).
        "followup_watcher.cancel()",
    )
    chokepoint_src = inspect.getsource(subagent_mod.SubagentManager._cancel_task_intentionally)
    assert "task.cancel()" in chokepoint_src
    for rel, lineno, line in raw_sites:
        assert any(s in line for s in allowed_substrings), (
            f"raw .cancel() at {rel}:{lineno} ({line!r}) — route it "
            "through _cancel_task_intentionally with a terminal marker"
        )
    # The generic 'task.cancel()' form must appear ONLY inside the chokepoint.
    generic = [
        (rel, n, line)
        for rel, n, line in raw_sites
        if "task.cancel()" in line
        and "_reaper_task" not in line
        and "reconcile_task" not in line
        and "recovery_task" not in line
        and "report_task" not in line
        and "lease_task" not in line
        and "submit_task" not in line
    ]
    assert len(generic) == 1, (
        f"expected exactly one raw task.cancel() (the chokepoint body), " f"found: {generic}"
    )


def test_chokepoint_unmarked_cancel_consumes_recovery_budget():
    """An intentional cancel issued WITHOUT a terminal marker must not be able
    to zombie-respawn: the chokepoint consumes the recovery budget
    defensively (and still cancels)."""
    mgr = _manager(_mock_sessions(lambda *a, **kw: None))
    info = SubagentInfo(id="sa-test", task="t", started=0.0)
    task = MagicMock()
    assert info._cancel_retry_used is False
    mgr._cancel_task_intentionally(task, info, reason="test-unmarked")
    task.cancel.assert_called_once()
    assert info._cancel_retry_used is True  # recovery can never fire now

    # Marked path: budget untouched.
    info2 = SubagentInfo(id="sa-test2", task="t", started=0.0)
    info2.user_stopped = True
    task2 = MagicMock()
    mgr._cancel_task_intentionally(task2, info2, reason="test-marked")
    task2.cancel.assert_called_once()
    assert info2._cancel_retry_used is False


def test_outcome_property_is_canonical_three_way():
    """SubagentInfo.outcome is THE single classification source: stopped wins
    over error-nullability; error means failed; neither means completed."""
    from kiro_crew.subagent import SubagentInfo

    stopped = SubagentInfo(id="o1", task="t")
    stopped.user_stopped = True
    assert stopped.outcome == "stopped"

    failed = SubagentInfo(id="o2", task="t")
    failed.error = "boom"
    assert failed.outcome == "failed"

    completed = SubagentInfo(id="o3", task="t")
    assert completed.outcome == "completed"


@pytest.mark.asyncio
async def test_reconcile_multiple_orphans_sends_single_digest_dm():
    """N orphans on the DM-fallback path produce ONE digest, never N pings."""
    dm = AsyncMock(return_value=True)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_dm=dm)

    orphans = [
        {"id": "orph-1", "pid": None, "parent_session": "", "task": "task one"},
        {"id": "orph-2", "pid": None, "parent_session": "", "task": "task two"},
        {"id": "orph-3", "pid": None, "parent_session": "", "task": "task three"},
    ]
    with (
        patch("kiro_crew.subagent.list_orphans", return_value=orphans),
        patch("kiro_crew.subagent.write_tombstone"),
        patch("kiro_crew.subagent.sel"),
    ):
        await mgr._reconcile_orphans()

    dm.assert_awaited_once()
    digest = dm.await_args.args[0]
    assert "3 subagent(s)" in digest
    for aid in ("orph-1", "orph-2", "orph-3"):
        assert aid in digest


@pytest.mark.asyncio
async def test_reconcile_single_orphan_dm_is_not_wrapped_in_digest():
    """A lone orphan's DM keeps the plain per-agent message (no digest header)."""
    dm = AsyncMock(return_value=True)
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_orphan_dm=dm)

    orphans = [{"id": "solo-1", "pid": None, "parent_session": "", "task": "solo task"}]
    with (
        patch("kiro_crew.subagent.list_orphans", return_value=orphans),
        patch("kiro_crew.subagent.write_tombstone"),
        patch("kiro_crew.subagent.sel"),
    ):
        await mgr._reconcile_orphans()

    dm.assert_awaited_once()
    msg = dm.await_args.args[0]
    assert "solo-1" in msg
    assert "restart digest" not in msg
