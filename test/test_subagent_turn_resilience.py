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
   Mirrors the main path's cancel recovery.
4. Orphan-notification wiring: ``_try_inject_orphan_notification`` /
   ``_send_orphan_slack_dm`` delegate to the gateway-wired callbacks instead of
   being stubs.
"""

from __future__ import annotations

import asyncio
import time
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


def _context_overflow_error():
    from kiro_crew.acp.client import AcpError

    error = AcpError("The agent context exceeded the model window", transient=False)
    error.structural_terminal = True
    error.context_overflow = True
    return error


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
    # Read synchronously after every turn; as AsyncMock children they
    # would hand back coroutines nobody awaits.
    provider.context_window_tokens = lambda: 0
    provider.context_used_tokens = lambda: 0
    provider.mcp_session_report = MagicMock(return_value=None)
    provider.stream = MagicMock(side_effect=stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    sessions.forget_conversation_if_sid = MagicMock(return_value=(False, None))
    sessions.aflush = AsyncMock()
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


@pytest.mark.asyncio
async def test_default_budget_allows_work_past_one_hundred_tools():
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

    async def stream(*_args, **_kwargs):
        for request_id in range(101):
            yield LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="read bounded input",
                request_id=request_id,
                tool_kind="mcp",
            )
        yield _text_event("verified result")
        yield _complete_event()

    manager = _manager(_mock_sessions(stream))
    info = await _spawn_and_wait(manager)
    assert info.error == ""
    assert info.result == "verified result"
    assert info.turns == 101


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
async def test_registration_rate_limited_death_retries_pretoken_and_recovers():
    """A pre-token AcpRegistrationRateLimited (runtime death whose stderr shows
    a throttled dynamic registration) rides the same zero-activity ladder as any
    transient: the SAME prompt is re-sent after backoff and the run completes,
    instead of surfacing a terminal generic process death."""
    from kiro_crew.acp.client import registration_rate_limited_error

    calls: list[str] = []
    throttled = registration_rate_limited_error(
        "Runtime process died during prompt",
        "Dynamic registration failed: Registration failed: HTTP 429 Too Many Requests",
    )

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) <= 2:
                raise throttled
            yield _text_event("registered and recovered")
            yield _complete_event()

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    # The typed message names a throttle, so the dependency adapters would
    # classify it and park the run on a coordinator wake this harness does not
    # drive; a null coordinator pins the IN-TURN ladder, which is the seam
    # under test.
    mgr.dependency_coordinator_async = AsyncMock(return_value=None)
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.error == ""
    assert "registered and recovered" in info.result
    # Zero activity on every failed attempt: the original prompt is replayed,
    # never a continuation that could assume prior work.
    assert calls == ["built_message"] * 3


@pytest.mark.asyncio
async def test_registration_rate_limited_exhaustion_surfaces_typed_message():
    """Persistent registration throttling fails after the bounded budget with
    the typed message (guidance, one retained cause) — not a stderr wall."""
    from kiro_crew.acp.client import registration_rate_limited_error

    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise registration_rate_limited_error(
                "Runtime process died during prompt",
                "Dynamic registration failed: Registration failed: HTTP 429 Too Many Requests",
            )
            yield  # noqa: unreachable — async generator marker

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    # Same in-turn pin as the recovery test above: the message would otherwise
    # classify as a dependency signal and wait on an undriven coordinator.
    mgr.dependency_coordinator_async = AsyncMock(return_value=None)
    with (
        patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0),
        patch("kiro_crew.subagent.configured_fallback_chain", return_value=()),
    ):
        info = await _spawn_and_wait(mgr)

    assert info.done is True
    assert "rate-limited" in info.error
    assert "retry later" in info.error
    assert len(calls) == 1 + TRANSIENT_RETRIES  # initial + bounded retries


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
    # The terminal error text names the WHOLE walk, not just the
    # last candidate's failure — the chain story is appended to info.error.
    assert "primary-model throttled" in info.error
    assert "fb-1" in info.error and "also unavailable" in info.error
    assert len(calls) == 1 + TRANSIENT_RETRIES + FALLBACK_CANDIDATE_ATTEMPTS
    provider.set_model.assert_awaited_once_with("fb-1")


@pytest.mark.asyncio
async def test_throttle_fallback_ladder_routes_through_shared_budget_body():
    """DRIFT PIN: the ladder must consult
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
    from kiro_crew.process_identity import MAX_ERROR_DETAIL_LEN

    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise _TransientError("backend throttle 500 " + "x" * (3 * MAX_ERROR_DETAIL_LEN))
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
    assert len(info.error) <= MAX_ERROR_DETAIL_LEN
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


def test_context_overflow_marker_forces_a_dedicated_runtime():
    sessions = MagicMock()
    sessions.is_session_sharing_eligible = MagicMock(return_value=True)
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
    info = SubagentInfo(
        id="overflow-gate",
        task="evaluate bounded evidence",
        parent_session_key="dashboard:parent",
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(session_sharing=True))

    with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
        assert mgr._should_use_session_sharing(info) is True
        info._force_dedicated = True
        assert mgr._should_use_session_sharing(info) is False


@pytest.mark.asyncio
async def test_first_turn_context_overflow_retries_once_on_a_dedicated_runtime():
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts: list[tuple[bool, bool, int | None, float | None]] = []
    events: list[tuple[str, dict]] = []
    timeline: list[str] = []
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()
    flush_entered = asyncio.Event()
    release_flush = asyncio.Event()
    capacity_wait_entered = asyncio.Event()
    release_capacity_wait = asyncio.Event()
    real_sleep = asyncio.sleep
    stale_shared_pid = 4242
    # A first attempt that waited long enough for its start clock to be past
    # the startup deadline by the time the recovery reaches its capacity wait.
    stale_exec_started = time.time() - (mgr._startup_deadline + 1_000)

    async def shutdown_shared_provider() -> None:
        timeline.append("teardown")

    shared_provider.shutdown = AsyncMock(side_effect=shutdown_shared_provider)

    async def on_event(etype: str, _info: SubagentInfo, extra: dict) -> None:
        events.append((etype, dict(extra)))

    mgr._on_event = on_event

    async def capacity_wait_sleep(delay: float) -> None:
        if mgr._max_concurrent == 0 and not capacity_wait_entered.is_set():
            capacity_wait_entered.set()
            await release_capacity_wait.wait()
            return
        await real_sleep(delay)

    def forget_conversation_if_sid(
        _session_key: str, _expected_sid: str
    ) -> tuple[bool, str | None]:
        timeline.append("delete")
        return True, "rejected-sid"

    async def flush_sessions() -> None:
        timeline.append("aflush")
        # The delete is not publishable until this barrier returns. While it is
        # held, recovery must retain the rejected attempt's identity and must
        # not wait for capacity, allocate a task, or emit recovery.
        assert info._session_sharing is True
        assert info._shared_provider is shared_provider
        assert info._pid == stale_shared_pid
        assert info._exec_started == stale_exec_started
        assert info._startup_deadline_stamp == (
            stale_exec_started,
            mgr._startup_deadline,
        )
        assert mgr._tasks.get(info.id) is None
        flush_entered.set()
        await release_flush.wait()

    sessions.forget_conversation_if_sid = MagicMock(side_effect=forget_conversation_if_sid)
    sessions.aflush = AsyncMock(side_effect=flush_sessions)

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        attempts.append(
            (info._force_dedicated, info._session_sharing, info._pid, info._exec_started)
        )
        if len(attempts) == 1:
            info._session_sharing = True
            info._shared_provider = shared_provider
            info._pid = stale_shared_pid
            # ``_run_inner_impl`` captures the acquired session's identity
            # before prompt dispatch; recovery retires exactly that SID.
            info._session_id = "rejected-sid"
            # The first attempt's startup clock, as ``_run_inner_impl`` and the
            # watchdog's deadline stamp leave it on the record.
            info._exec_started = stale_exec_started
            info._startup_deadline_stamp = (stale_exec_started, mgr._startup_deadline)
            # Hold the recovery at its capacity wait. The retired process
            # identity must be cleared before this wait can suspend.
            mgr._max_concurrent = 0
            raise _context_overflow_error()
        timeline.append("replacement")
        replacement_started.set()
        await release_replacement.wait()
        info.result = "recovered on dedicated runtime"
        info.done = True

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.asyncio.sleep", side_effect=capacity_wait_sleep),
    ):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        await first

        await asyncio.wait_for(flush_entered.wait(), timeout=_RESPAWN_TIMEOUT)
        assert timeline == ["teardown", "delete", "aflush"]
        assert capacity_wait_entered.is_set() is False
        assert replacement_started.is_set() is False
        assert [etype for etype, _extra in events].count("subagent_recovering") == 0

        release_flush.set()
        await asyncio.wait_for(capacity_wait_entered.wait(), timeout=_RESPAWN_TIMEOUT)
        assert info._session_sharing is False
        assert info._shared_provider is None
        assert info._pid is None
        # Both startup clocks are cleared with the PID, before the wait could
        # suspend: the replacement has not entered ``_run_inner``, so the record
        # reads as a run that has not started and the startup watchdog cannot
        # reap the capacity wait as a stalled start.
        assert info._exec_started is None
        assert info._startup_deadline_stamp is None
        assert info.turns == 0 and info._first_stream_started is None
        now = time.time()
        assert mgr._is_startup_stalled(info, now) is False
        # The same record with the FIRST attempt's clock left in place is what
        # the watchdog would have reaped, so the assertion above is not vacuous.
        stale = SubagentInfo(id="stale-clock", task=info.task, parent_session_key="")
        stale._exec_started = stale_exec_started
        stale._startup_deadline_stamp = (stale_exec_started, mgr._startup_deadline)
        assert mgr._is_startup_stalled(stale, now) is True
        assert replacement_started.is_set() is False

        mgr._max_concurrent = 1
        release_capacity_wait.set()
        await asyncio.wait_for(replacement_started.wait(), timeout=_RESPAWN_TIMEOUT)
        replacement = mgr._tasks.get(info.id)
        assert replacement is not None and replacement is not first
        release_replacement.set()
        await replacement

    assert attempts == [(False, False, None, None), (True, False, None, None)]
    assert timeline == ["teardown", "delete", "aflush", "replacement"]
    sessions.aflush.assert_awaited_once_with()
    assert info.done is True
    assert info.error == ""
    assert info.result == "recovered on dedicated runtime"
    assert info._context_overflow_retry_used is True
    shared_provider.shutdown.assert_awaited_once()
    recovering = [extra for etype, extra in events if etype == "subagent_recovering"]
    assert recovering == [{"attempt": 1}]


@pytest.mark.asyncio
async def test_dedicated_first_attempt_context_overflow_is_terminal():
    """A first spawn whose own dedicated process overflowed before any activity.

    Eligibility is shared-first-attempt only: after a dedicated first attempt
    no transition is evidenced that would be expected to make a replacement's
    envelope fit, so another teardown and spawn cycle is not justified. The
    overflow is terminal without scheduling recovery and without consuming the
    one-shot.
    """
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    schedule_recovery = MagicMock()
    mgr._schedule_cancel_recovery = schedule_recovery

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        # A dedicated first attempt: no shared handle, its own process.
        assert info._session_sharing is False and info._shared_provider is None
        assert info.conversation_key == "" and info.turns == 0
        info._pid = 5151
        info._session_id = "rejected-sid"
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        await mgr._tasks[info.id]
        await asyncio.sleep(0)

    assert attempts == 1
    assert info.done is True
    assert "exceeded the model window" in info.error
    assert info._context_overflow_retry_used is False
    assert info._force_dedicated is False
    schedule_recovery.assert_not_called()
    assert mgr._tasks.get(f"{info.id}:recovery") is None
    sessions.forget_conversation_if_sid.assert_not_called()
    sessions.aflush.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_turn_context_overflow_forgets_rejected_sid_before_fresh_recovery():
    """A fresh run must not session/load its rejected shared first-attempt SID.

    The first attempt runs on the shared runtime through the real ``_run_inner``
    (the shared session is stubbed where the parent's runtime would create it);
    the replacement is the ordinary dedicated allocation.
    """
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    # Shared for the first attempt, dedicated once the one-shot forces it; the
    # rest of the plan (template, no pin) stays as the real rule reads it.
    mgr._should_use_session_sharing = MagicMock(side_effect=lambda info: not info._force_dedicated)
    provider = sessions._provider
    provider.session_id = ""
    provider.cwd = ""
    provider.set_keep_transcript = MagicMock()
    mapping: dict[str, str] = {}
    timeline: list[str] = []
    wire: list[tuple[str, str]] = []
    events: list[str] = []
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()

    async def on_event(etype: str, _info: SubagentInfo, _extra: dict) -> None:
        events.append(etype)

    mgr._on_event = on_event

    async def create_shared_session(info: SubagentInfo, session_key: str, _agent: str):
        # Stands in for ``_bind_shared_handle``: the session is created on the
        # shared runtime, its identity recorded and a resumable mapping persisted.
        mapping[session_key] = "rejected-sid"
        timeline.append("session/new:rejected-sid")
        wire.append(("session/new", "rejected-sid"))
        provider.session_id = "rejected-sid"
        info._session_sharing = True
        info._shared_provider = provider
        return provider

    async def get_or_create(session_key: str, **_kwargs):
        if session_key in mapping:
            sid = mapping[session_key]
            timeline.append(f"session/load:{sid}")
            wire.append(("session/load", sid))
            provider.session_id = sid
            return provider, True, True
        sid = "recovered-sid"
        mapping[session_key] = sid
        timeline.append(f"session/new:{sid}")
        wire.append(("session/new", sid))
        provider.session_id = sid
        return provider, True, False

    async def shutdown_shared() -> None:
        timeline.append(f"shutdown:{provider.session_id}")

    async def flush_sessions() -> None:
        timeline.append("aflush")

    def forget_conversation_if_sid(session_key: str, expected_sid: str) -> tuple[bool, str | None]:
        current_sid = mapping.get(session_key)
        timeline.append(f"forget-if:{expected_sid}:{current_sid or ''}")
        if current_sid is None:
            return False, None
        if current_sid != expected_sid:
            return False, current_sid
        mapping.pop(session_key)
        return True, current_sid

    def stream_factory(_message: str, *_args, **_kwargs):
        sid = provider.session_id

        async def stream():
            if sid == "rejected-sid":
                raise _context_overflow_error()
            replacement_started.set()
            await release_replacement.wait()
            yield _text_event("recovered with fresh native context")
            yield _complete_event()

        return stream()

    mgr._create_shared_session = AsyncMock(side_effect=create_shared_session)
    sessions.get_or_create = AsyncMock(side_effect=get_or_create)
    provider.shutdown = AsyncMock(side_effect=shutdown_shared)
    sessions.forget_conversation_if_sid = MagicMock(side_effect=forget_conversation_if_sid)
    sessions.aflush = AsyncMock(side_effect=flush_sessions)
    provider.stream = MagicMock(side_effect=stream_factory)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        session_key = f"subagent:{info.id}"
        first = mgr._tasks[info.id]
        await first

        await asyncio.wait_for(replacement_started.wait(), timeout=_RESPAWN_TIMEOUT)
        replacement = mgr._tasks.get(info.id)
        assert replacement is not None and replacement is not first
        release_replacement.set()
        await replacement

    assert wire == [
        ("session/new", "rejected-sid"),
        ("session/new", "recovered-sid"),
    ]
    assert timeline.index("shutdown:rejected-sid") < timeline.index(
        "forget-if:rejected-sid:rejected-sid"
    )
    assert timeline.index("forget-if:rejected-sid:rejected-sid") < timeline.index("aflush")
    assert timeline.index("aflush") < timeline.index("session/new:recovered-sid")
    sessions.forget_conversation_if_sid.assert_called_once_with(session_key, "rejected-sid")
    sessions.aflush.assert_awaited_once_with()
    # The first attempt's teardown shut its shared handle; the replacement's
    # own dedicated process is what the later reset belongs to.
    provider.shutdown.assert_awaited_once()
    assert mapping == {session_key: "recovered-sid"}
    assert info.done is True
    assert info.error == ""
    assert info.result == "recovered with fresh native context"
    assert info._session_id == "recovered-sid"
    assert info._session_sharing is False
    assert events.count("subagent_recovering") == 1
    assert events.count("subagent_done") == 1


@pytest.mark.asyncio
async def test_context_overflow_recovery_preserves_a_successor_sid_and_fails_closed():
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        info._session_sharing = True
        info._shared_provider = shared_provider
        info._session_id = "rejected-sid"
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    sessions.forget_conversation_if_sid = MagicMock(return_value=(False, "successor-sid"))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        await first
        recovery = mgr._tasks.get(f"{info.id}:recovery")
        assert recovery is not None
        await asyncio.wait_for(
            asyncio.gather(recovery, return_exceptions=True), timeout=_RESPAWN_TIMEOUT
        )

    assert attempts == 1
    sessions.forget_conversation_if_sid.assert_called_once_with(
        f"subagent:{info.id}", "rejected-sid"
    )
    assert info.done is True
    assert info.error == (
        "agent context exceeded the model window and the dedicated-session "
        "recovery could not start"
    )
    assert mgr._tasks.get(info.id) is None
    sessions.aflush.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_overflow_recovery_flush_failure_is_terminal_before_replacement():
    on_done = AsyncMock()
    sessions = _mock_sessions(lambda _msg: None)
    mgr = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        on_done=on_done,
    )
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    attempts = 0
    events: list[str] = []
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def on_event(etype: str, _info: SubagentInfo, _extra: dict) -> None:
        events.append(etype)

    mgr._on_event = on_event

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise AssertionError("replacement allocated after durability failure")
        info._session_sharing = True
        info._shared_provider = shared_provider
        info._session_id = "rejected-sid"
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    sessions.forget_conversation_if_sid = MagicMock(return_value=(True, "rejected-sid"))
    sessions.aflush = AsyncMock(side_effect=OSError("session map fsync failed"))

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        await first
        recovery = mgr._tasks.get(f"{info.id}:recovery")
        assert recovery is not None
        await asyncio.wait_for(
            asyncio.gather(recovery, return_exceptions=True),
            timeout=_RESPAWN_TIMEOUT,
        )
        replacement = mgr._tasks.get(info.id)
        if replacement is not None:
            await asyncio.gather(replacement, return_exceptions=True)

    assert attempts == 1
    sessions.forget_conversation_if_sid.assert_called_once_with(
        f"subagent:{info.id}", "rejected-sid"
    )
    sessions.aflush.assert_awaited_once_with()
    assert mgr._tasks.get(info.id) is None
    assert events.count("subagent_recovering") == 0
    assert info.done is True
    assert info.error == (
        "agent context exceeded the model window and the dedicated-session "
        "recovery could not start"
    )
    assert events.count("subagent_done") == 1
    on_done.assert_awaited_once_with(info)


@pytest.mark.asyncio
async def test_context_overflow_recovery_without_a_rejected_sid_fails_closed():
    """No rejected SID: recovery stops before any capacity wait or allocation.

    The identity capture after session acquisition is best-effort, while the
    allocation may already have persisted a resumable mapping. With no SID to
    compare, recovery cannot tell a mapped rejected attempt from a successor,
    so it must neither touch the mapping nor allocate a replacement that could
    ``session/load`` it.
    """
    on_done = AsyncMock()
    sessions = _mock_sessions(lambda _msg: None)
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder(), on_done=on_done)
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    attempts = 0
    events: list[str] = []
    mapping: dict[str, str] = {}
    capacity_wait_entered = asyncio.Event()
    real_sleep = asyncio.sleep
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def on_event(etype: str, _info: SubagentInfo, _extra: dict) -> None:
        events.append(etype)

    mgr._on_event = on_event

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        # The acquisition persisted a resumable mapping for this key, but the
        # best-effort identity capture never landed on the record.
        assert not getattr(info, "_session_id", "")
        info._session_sharing = True
        info._shared_provider = shared_provider
        mapping[f"subagent:{info.id}"] = "mapped-sid"
        # A full pool: a recovery that reaches its capacity wait would suspend
        # here, so the probe below distinguishes "never waited" from "waited".
        mgr._max_concurrent = 0
        raise _context_overflow_error()

    async def capacity_wait_sleep(delay: float) -> None:
        if mgr._max_concurrent == 0:
            capacity_wait_entered.set()
        await real_sleep(delay)

    def forget_conversation_if_sid(session_key: str, expected_sid: str) -> tuple[bool, str | None]:
        current_sid = mapping.get(session_key)
        if current_sid == expected_sid:
            mapping.pop(session_key)
            return True, current_sid
        return False, current_sid

    mgr._run_inner = AsyncMock(side_effect=run_inner)
    sessions.forget_conversation_if_sid = MagicMock(side_effect=forget_conversation_if_sid)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.asyncio.sleep", side_effect=capacity_wait_sleep),
    ):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        session_key = f"subagent:{info.id}"
        first = mgr._tasks[info.id]
        await first
        # The overflow was accepted for recovery (not finalized by the run) and
        # the recovery coroutine is pending after the original's teardown.
        assert info._context_overflow_retry_used is True
        recovery = mgr._tasks.get(f"{info.id}:recovery")
        assert recovery is not None
        await asyncio.wait_for(
            asyncio.gather(recovery, return_exceptions=True), timeout=_RESPAWN_TIMEOUT
        )

    # Fail-closed BEFORE the capacity wait and before any replacement.
    assert attempts == 1
    assert capacity_wait_entered.is_set() is False
    assert mgr._tasks.get(info.id) is None
    assert mgr._tasks.get(f"{info.id}:recovery") is None
    assert events.count("subagent_recovering") == 0
    # No mapping was deleted or altered: without the rejected SID the mapped
    # entry cannot be proven to be the rejected attempt rather than a successor.
    sessions.forget_conversation_if_sid.assert_not_called()
    sessions.aflush.assert_not_awaited()
    assert mapping == {session_key: "mapped-sid"}
    # Terminal recovery failure, finalized and reported exactly once.
    assert info.done is True
    assert info.error == (
        "agent context exceeded the model window and the dedicated-session "
        "recovery could not start"
    )
    assert events.count("subagent_done") == 1
    on_done.assert_awaited_once_with(info)


@pytest.mark.asyncio
async def test_continuation_context_overflow_before_activity_is_terminal():
    import kiro_crew.subagent_persistence as sp

    await asyncio.to_thread(
        sp.create_agent_folder,
        "original-run",
        memory_mode="persistent",
    )
    await asyncio.to_thread(sp.write_run_agent, "original-run", "")
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    schedule_recovery = MagicMock()
    mgr._schedule_cancel_recovery = schedule_recovery

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        assert info.conversation_key == "subagent:original-run"
        assert info.turns == 0
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(
            "continue established conversation",
            keep=True,
            conversation_key="subagent:original-run",
        )
        assert info is not None
        await mgr._tasks[info.id]

    assert attempts == 1
    assert info.done is True
    assert "exceeded the model window" in info.error
    assert info._context_overflow_retry_used is False
    assert info._force_dedicated is False
    schedule_recovery.assert_not_called()
    assert mgr._tasks.get(f"{info.id}:recovery") is None


@pytest.mark.asyncio
async def test_dedicated_context_overflow_is_terminal_without_a_retry_loop():
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            info._session_sharing = True
            info._shared_provider = shared_provider
            info._session_id = "rejected-sid"
        else:
            replacement_started.set()
            await release_replacement.wait()
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        await first

        await asyncio.wait_for(replacement_started.wait(), timeout=_RESPAWN_TIMEOUT)
        replacement = mgr._tasks.get(info.id)
        assert replacement is not None and replacement is not first
        release_replacement.set()
        await replacement
        await asyncio.sleep(0)

    assert attempts == 2
    assert info.done is True
    assert info.error.startswith("Dedicated-session recovery also overflowed:")
    assert mgr._tasks.get(f"{info.id}:recovery") is None
    shared_provider.shutdown.assert_awaited_once()


@pytest.mark.parametrize(
    ("field", "value", "side_effecting"),
    [
        ("result", "partial result", False),
        ("streaming_text", "partial stream", False),
        ("tool_count", 1, True),
        ("turns", 1, False),
    ],
)
@pytest.mark.asyncio
async def test_context_overflow_after_activity_is_not_replayed(
    field: str, value: object, side_effecting: bool
):
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    attempts = 0
    shared_provider = MagicMock()
    shared_provider.set_keep_transcript = MagicMock()
    shared_provider.shutdown = AsyncMock()

    async def run_inner(info: SubagentInfo, _session_key: str) -> None:
        nonlocal attempts
        attempts += 1
        info._session_sharing = True
        info._shared_provider = shared_provider
        setattr(info, field, value)
        raise _context_overflow_error()

    mgr._run_inner = AsyncMock(side_effect=run_inner)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        await mgr._tasks[info.id]
        await asyncio.sleep(0)

    assert attempts == 1
    assert info.done is True
    assert "was not replayed" in info.error
    if side_effecting:
        # A tool ran: the fresh runtime cannot know what it changed, so the
        # error names the side-effect risk, as the unexpected-cancel gate does.
        assert "after tools executed" in info.error
        assert "could repeat side effects" in info.error
    else:
        # Text, a result or a turn without a tool call changed no state: the
        # error says the work was preserved and replay would duplicate it,
        # and must not claim a side effect that never happened.
        assert "had already produced work" in info.error
        assert "preserved" in info.error
        assert "duplicating" in info.error
        assert "side effect" not in info.error
        assert "tools executed" not in info.error
    assert info._context_overflow_retry_used is False
    assert mgr._tasks.get(f"{info.id}:recovery") is None
    if field == "streaming_text":
        assert info.result == value
    shared_provider.shutdown.assert_awaited_once()


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
    # The structured completion facts are forwarded as a third arg;
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

    async def _slow_reset(key, **_):
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
    Allowed raw sites: the chokepoint body itself, the reaper-loop task, and a
    pending cancel-recovery scheduler task — none of the latter two are managed
    runs, so the marker contract (and recovery) never applies to them."""
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
        # A reap supersedes a pending respawn; the recovery task schedules the
        # respawn and is NOT a managed run, so no terminal marker applies.
        "recovery_task.cancel()",
        # Shielded terminal-report tasks drained at shutdown — also not managed
        # runs; cancelling them cannot trigger a respawn.
        "report_task.cancel()",
        # follow_up watchers (spawn_steer mode="follow_up") — observers, not
        # managed runs: no terminal marker applies, and cancelling one cannot
        # trigger a respawn (it only ever DISPATCHES via continue_conversation,
        # which cancel_all pre-empts by cancelling watchers first).
        "followup_watcher.cancel()",
        # The pending async OPEN of the durable task store, cancelled by ``close()``.
        # It is a store-open task, not a managed run: no terminal marker applies and
        # cancelling it cannot trigger a respawn. Left pending it would complete after
        # the close and re-attach the connection this method exists to release.
        "taskq_open_task.cancel()",
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
        and "recovery_task" not in line
        and "report_task" not in line
        and "taskq_open_task" not in line
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


def _streams_then_fails(error: Exception, text: str = "the answer "):
    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            if text:
                yield _text_event(text)
            raise error

        return _gen()

    return stream_factory


_GENERATE_FAILED = "The model failed to generate a response (transient error)."


@pytest.mark.asyncio
@pytest.mark.parametrize("keep", ["head", "tail"])
async def test_generate_failed_after_output_keeps_the_output_with_a_warning(keep):
    """Output already streamed survives a transient generate failure the retry cannot fix.

    Longer than the keep cap, so a warning added before the cap would be cut off."""
    error, text = _TransientError(_GENERATE_FAILED), "x" * 4000
    calls: list[str] = []
    factory = _streams_then_fails(error, text)

    def _recording(msg: str, *a, **kw):
        calls.append(msg)
        return factory(msg, *a, **kw)

    mgr = _manager(_mock_sessions(_recording))
    mgr.update_completion_keep(keep, 3000)
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.outcome == "completed"
    assert not info.error
    assert info.partial is True
    assert info.result.startswith("_Warning: the backend failed to generate")
    assert "xxxx" in info.result
    # The output is kept only after the one continue turn was tried.
    assert len(calls) == 2 and calls[1] == _TRANSIENT_CONTINUE_MSG
    # The disk copy, read by spawn_status / spawn_run, carries the warning too.
    from pathlib import Path

    assert info.result_path
    assert "_Warning: the backend failed to generate" in Path(info.result_path).read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "text"),
    [
        (_TransientError("500 mid-stream"), "the answer "),  # transient, not generate-failed
        (_FatalError(_GENERATE_FAILED), "the answer "),  # not transient
        (_TransientError(_GENERATE_FAILED), " "),  # whitespace-only output
    ],
)
async def test_other_failures_after_output_still_fail(error, text):
    mgr = _manager(_mock_sessions(_streams_then_fails(error, text)))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.outcome == "failed"
    assert info.error


@pytest.mark.asyncio
async def test_control_tag_only_output_still_fails():
    """Output that is only an [OPTIONS: ...] tag is empty once the tag is stripped."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                yield _text_event("[OPTIONS: Retry | Stop]")
            raise _TransientError(_GENERATE_FAILED)

        return _gen()

    mgr = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    assert info.outcome == "failed"
    assert info.error
