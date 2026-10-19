"""Sub-agent transient-backend retry and provider throttling (``subagent.py``).

The cases under the ``1.`` and ``1b.`` section banners, among them the pre-token re-prompt
and the post-token CONTINUE, the retry budget, the throttle fallback chain, the timeout and
deadline a run captures across a recovery, and a provider throttle while the durable task
queue is open; and the default budget letting a run pass one hundred tool requests. The
shared helpers come from ``subagent_turn_resilience_helpers.py``.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import wait_taskq_open
from subagent_turn_resilience_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _complete_event,
    _context_overflow_error,
    _FatalError,
    _manager,
    _mock_sessions,
    _spawn_and_wait,
    _text_event,
    _TransientError,
)

from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.llm_helpers import TRANSIENT_RETRIES
from kiro_crew.providers.base import EVENT_COMPLETE
from kiro_crew.subagent import (
    _TRANSIENT_CONTINUE_MSG,
    SubagentInfo,
    SubagentManager,
)
from kiro_crew.testing.wait import async_wait_until, await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


class _TimeoutCapableProvider:
    """Provider double whose type explicitly opts into captured prompt timeouts."""

    def __init__(self, stream_factory) -> None:
        self._stream_factory = stream_factory
        self.backend = ""
        self.cwd = ""
        self.last_infra_error = None
        self.session_id = ""
        self.served_model = ""
        self._model = ""
        self.approve_tool = AsyncMock()
        self.reject_tool = AsyncMock()
        self.set_model = AsyncMock()
        self.available_models = MagicMock(return_value=[])

    def prompt_timeout_for_deadline(self, deadline: float) -> float:
        from kiro_crew.acp.client import resolve_prompt_timeout_for_deadline

        return resolve_prompt_timeout_for_deadline(deadline)

    def stream(self, message: str, timeout: float | None = None):
        return self._stream_factory(message, timeout=timeout)

    def context_usage_pct(self) -> float:
        return 0.0

    def context_window_tokens(self) -> int:
        return 0

    def context_used_tokens(self) -> int:
        return 0


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
async def test_captured_timeout_survives_config_cut_across_all_recovery_prompts(monkeypatch):
    from kiro_crew.acp.client import _PROMPT_TIMEOUT_MARGIN_SECS, resolve_prompt_timeout
    from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.providers.base import LLMEvent

    cfg = KiroCrewConfig()
    cfg.agent.chat_turn_timeout_secs = 3600
    cfg.agent.subagent_timeout_secs = 21600
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    calls: list[tuple[str, float | None]] = []

    def stream_factory(message: str, *args, **kwargs):
        calls.append((message, kwargs.get("timeout")))

        async def _gen():
            if len(calls) == 1:
                raise _TransientError("backend 500")
            if len(calls) == 2:
                cfg.agent.subagent_timeout_secs = 10800
                yield _text_event("partial ")
                yield LLMEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_TOOL_STALL,
                    text="verdict=unknown; redirected_output=run.log",
                )
                return
            yield _text_event("finished")
            yield _complete_event()

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = _TimeoutCapableProvider(stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions._provider = provider
    mgr = _manager(sessions)
    mgr._default_timeout = 21600
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)

    expected = 21600.0 + _PROMPT_TIMEOUT_MARGIN_SECS
    assert info.error == ""
    assert [timeout for _, timeout in calls] == [expected, expected, expected]
    assert calls[0][0] == calls[1][0] == "built_message"
    assert calls[2][0] != "built_message"
    assert resolve_prompt_timeout() < expected


@pytest.mark.asyncio
async def test_cancel_recovery_preserves_captured_timeout_after_config_cut(monkeypatch):
    from kiro_crew.acp.client import _PROMPT_TIMEOUT_MARGIN_SECS, resolve_prompt_timeout
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent.chat_turn_timeout_secs = 3600
    cfg.agent.subagent_timeout_secs = 21600
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    calls: list[tuple[str, float | None]] = []
    first_started = asyncio.Event()
    recovery_started = asyncio.Event()
    finish_recovery = asyncio.Event()

    def stream_factory(message: str, *args, **kwargs):
        calls.append((message, kwargs.get("timeout")))

        async def _gen():
            if len(calls) == 1:
                yield _text_event("partial ")
                first_started.set()
                # flake-ok: pre-existing mocked-stream hang, moved verbatim; the test cancels it
                await asyncio.Event().wait()
            recovery_started.set()
            # flake-ok: pre-existing mocked-run event wait, moved verbatim; the test sets the event
            await finish_recovery.wait()
            yield _text_event("finished")
            yield _complete_event()

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = _TimeoutCapableProvider(stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions._provider = provider
    mgr = _manager(sessions)
    mgr._default_timeout = 21600

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("resumable six-hour job")
        assert info is not None
        assert info.timeout_secs == 0
        await await_until(first_started, what="the first run streaming")
        assert info.timeout_secs == 21600
        assert info.streaming_text and info.tool_count == 0

        # Live configuration affects genuinely fresh runs, never the immutable
        # deadline already captured by this same-info recovery.
        cfg.agent.subagent_timeout_secs = 10800
        mgr._default_timeout = 10800

        task1 = mgr._tasks[info.id]
        task1.cancel()
        # flake-ok: pre-existing unbounded gather of scheduled tasks, moved verbatim
        await asyncio.gather(task1, return_exceptions=True)
        await await_until(recovery_started, what="the recovery run streaming")
        task2 = mgr._tasks[info.id]

        expected = 21600.0 + _PROMPT_TIMEOUT_MARGIN_SECS
        assert task2 is not task1
        assert info.timeout_secs == 21600
        assert [timeout for _, timeout in calls] == [expected, expected]
        assert resolve_prompt_timeout() < expected

        finish_recovery.set()
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await task2

    assert info.error == ""
    assert info.done is True
    assert mgr._running_count == 0


@pytest.mark.asyncio
async def test_fresh_run_captures_current_lowered_timeout(monkeypatch):
    from kiro_crew.acp.client import resolve_prompt_timeout_for_deadline
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent.chat_turn_timeout_secs = 3600
    cfg.agent.subagent_timeout_secs = 10800
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    calls: list[tuple[str, float | None]] = []

    def stream_factory(message: str, *args, **kwargs):
        calls.append((message, kwargs.get("timeout")))

        async def _gen():
            yield _complete_event()

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = _TimeoutCapableProvider(stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions._provider = provider
    mgr = _manager(sessions)
    mgr._default_timeout = 10800

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("fresh lowered-deadline job")
        assert info is not None
        assert info.timeout_secs == 0
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await mgr._tasks[info.id]

    expected = resolve_prompt_timeout_for_deadline(10800.0)
    assert info.timeout_secs == 10800
    assert [timeout for _, timeout in calls] == [expected]
    assert info.done is True
    assert mgr._running_count == 0


class _SweepDone(BaseException):
    """Ends ``_reaper_loop`` after exactly one full sweep.

    A ``BaseException`` so the loop's own ``except Exception`` guards let it out.
    """


async def _one_reaper_sweep(monkeypatch, mgr: SubagentManager) -> list[str]:
    """Run exactly one real ``_reaper_loop`` sweep and return the ids it reaped.

    ``_force_reap`` is a recorder that only marks the run done, and the sweep's
    unrelated passes are stubbed, so the wall-clock check is what decides.
    """
    import kiro_crew.subagent as subagent_mod

    reaped: list[str] = []

    async def _force_reap(agent_id, info, _elapsed, *, reason=""):
        reaped.append(agent_id)
        info.done = True

    compactions: list[int] = []

    def _compact() -> None:
        # Call 1 is the startup trim and call 2 opens the first sweep, so call
        # 3 can only come after that sweep finished.
        compactions.append(1)
        if len(compactions) == 3:
            raise _SweepDone

    monkeypatch.setattr(mgr, "_force_reap", _force_reap)
    monkeypatch.setattr(mgr, "_rebuild_conversation_registry", AsyncMock())
    monkeypatch.setattr(mgr, "_sample_live_costs", MagicMock())
    monkeypatch.setattr(mgr, "_refresh_learned_settled", MagicMock())
    monkeypatch.setattr(mgr, "_sweep_stuck_waves_async", AsyncMock())
    monkeypatch.setattr(mgr, "_sweep_digest_holds_async", AsyncMock())
    monkeypatch.setattr(mgr, "_sweep_conversations_async", AsyncMock())
    monkeypatch.setattr(mgr, "_taskq_pump", MagicMock())
    monkeypatch.setattr(mgr, "_maybe_flag_stall", AsyncMock())
    monkeypatch.setattr(subagent_mod, "_REAPER_INTERVAL", 0)
    monkeypatch.setattr(subagent_mod, "compact_cost_log", _compact)
    monkeypatch.setattr(subagent_mod, "prune_stale_tombstones", lambda *a, **k: 0)

    with pytest.raises(_SweepDone):
        # flake-ok: pre-existing await of the reaper loop, moved verbatim; the compact stub ends it
        await mgr._reaper_loop()
    return reaped


@pytest.mark.asyncio
async def test_reaper_enforces_the_captured_deadline_from_execution_start(monkeypatch):
    """One real sweep: the reaper measures each run's captured deadline from
    ``_exec_started_mono``, on the monotonic clock. A run inside its captured hour is left alone although a
    reload cut the manager default to one minute, a run past its captured hour
    is reaped, and a run that has not started executing keeps the bound it had
    before the capture existed: its registration age against the live default,
    so a long-parked start is reaped and a fresh one is not. A run between two
    recovery attempts (``_exec_started_mono`` cleared, deadline already captured) is
    measured on the same registration age against its captured deadline, not
    the cut default: inside it, it is left alone; past it, it is reaped."""
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), default_timeout=60)
    now = time.time()
    mono_now = time.monotonic()

    def _register(
        agent_id: str,
        exec_started_ago: float | None,
        timeout_secs: int,
        *,
        registered_ago: float = 4000.0,
    ) -> SubagentInfo:
        # The reaper reads the monotonic clock (``_started_mono``,
        # ``_exec_started_mono``); the wall-clock twins are set the same way for
        # the readers that still report them.
        info = SubagentInfo(
            id=agent_id,
            task="t",
            started=now - registered_ago,
            timeout_secs=timeout_secs,
            turns=1,
            _pid=123,
        )
        info._started_mono = mono_now - registered_ago
        if exec_started_ago is None:
            info._exec_started = None
            info._exec_started_mono = None
        else:
            info._exec_started = now - exec_started_ago
            info._exec_started_mono = mono_now - exec_started_ago
        info.last_activity = now
        mgr._agents[agent_id] = info
        return info

    inside = _register("inside", 120.0, 3600)
    past = _register("past", 4000.0, 3600)
    waiting = _register("waiting", None, 0)
    fresh = _register("fresh", None, 0, registered_ago=30.0)
    between = _register("between", None, 7200)
    between_past = _register("between-past", None, 3600)

    reaped = await _one_reaper_sweep(monkeypatch, mgr)

    assert reaped == ["past", "waiting", "between-past"]
    assert past.done is True
    assert waiting.done is True
    assert between_past.done is True
    assert inside.done is False
    assert fresh.done is False
    assert between.done is False


@pytest.mark.asyncio
async def test_reload_during_the_recovery_capacity_wait_keeps_the_captured_deadline(monkeypatch):
    """A context-overflow cancel recovery clears ``_exec_started`` before its
    replacement waits for capacity. A reload that cuts the manager default
    during that wait must not let a reaper sweep end the run on the cut
    default: the run keeps the deadline it captured, and the replacement runs
    on that same deadline once a slot frees."""
    sessions = _mock_sessions(lambda _msg: None)
    mgr = _manager(sessions)
    mgr._default_timeout = 21600
    # Shared for the first attempt, dedicated once the one-shot forces it.
    mgr._should_use_session_sharing = MagicMock(side_effect=lambda info: not info._force_dedicated)
    provider = sessions._provider
    provider.session_id = ""
    provider.cwd = ""
    provider.set_keep_transcript = MagicMock()
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()

    async def create_shared_session(info: SubagentInfo, _session_key: str, _agent: str):
        provider.session_id = "rejected-sid"
        info._session_sharing = True
        info._shared_provider = provider
        return provider

    async def get_or_create(_session_key: str, **_kwargs):
        provider.session_id = "recovered-sid"
        return provider, True, False

    def stream_factory(_message: str, *_args, **_kwargs):
        sid = provider.session_id

        async def stream():
            if sid == "rejected-sid":
                raise _context_overflow_error()
            replacement_started.set()
            # flake-ok: pre-existing mocked-run event wait, moved verbatim; the test sets the event
            await release_replacement.wait()
            yield _text_event("recovered on the captured deadline")
            yield _complete_event()

        return stream()

    mgr._create_shared_session = AsyncMock(side_effect=create_shared_session)
    sessions.get_or_create = AsyncMock(side_effect=get_or_create)
    sessions.forget_conversation_if_sid = MagicMock(return_value=(True, "rejected-sid"))
    provider.stream = MagicMock(side_effect=stream_factory)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("evaluate bounded evidence")
        assert info is not None
        first = mgr._tasks[info.id]
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await first
        # The freed slot is taken before the recovery re-checks capacity, so
        # the replacement waits in ``_RECOVERY_SLOT_WAIT_SECS``'s poll.
        mgr._max_concurrent = 1
        mgr._running_count = 1
        await async_wait_until(
            lambda: info._exec_started is None,
            describe=lambda: f"tasks={sorted(mgr._tasks)}; error={info.error!r}",
        )
        assert info._exec_started is None
        assert info._recovering is True
        assert mgr._tasks.get(info.id) is None
        assert f"{info.id}:recovery" in mgr._tasks
        assert info.timeout_secs == 21600

        # A reload cuts the default to one minute while the run's registration
        # age is past it and inside its captured six hours.
        mgr._default_timeout = 60
        info.started = time.time() - 4000.0
        reaped = await _one_reaper_sweep(monkeypatch, mgr)
        assert reaped == []
        assert info.done is False

        mgr._running_count = 0
        await await_until(replacement_started, what="the replacement run streaming")
        replacement = mgr._tasks.get(info.id)
        assert replacement is not None and replacement is not first
        assert info.timeout_secs == 21600
        release_replacement.set()
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await replacement

    assert info.error == ""
    assert info.result == "recovered on the captured deadline"
    assert info.done is True


@pytest.mark.parametrize(
    ("run_timeout", "named_deadline"),
    [
        pytest.param(10800, 10800, id="captured deadline after the default moved"),
        pytest.param(0, 16200, id="run that never captured one"),
    ],
)
@pytest.mark.asyncio
async def test_reap_record_names_the_deadline_the_reaper_enforced(run_timeout, named_deadline):
    """The reaper fires on ``info.timeout_secs or _default_timeout``; the record
    names that same deadline. A reload moves the manager default without moving
    a running run's captured deadline, so naming the default would claim 16200s
    for a run reaped at 10800s."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock(return_value=True)
    sessions.tearing_down = MagicMock(return_value=[])
    sessions._sessions = {}
    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock(), default_timeout=16200)
    now = time.time()
    info = SubagentInfo(
        id="reaped-at-its-own-deadline",
        task="test",
        started=now - 10870,
        timeout_secs=run_timeout,
    )
    info._exec_started = now - 10870
    manager._agents[info.id] = info
    manager._running_count = 1

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        await manager._force_reap(info.id, info, 10870.0)

    assert info.done and info.reaped
    assert info.error.startswith(f"Reaped after 10870s (exceeded {named_deadline}s deadline)")


@pytest.mark.asyncio
async def test_timeout_record_names_the_runs_captured_deadline():
    """``Timed out after`` names the deadline ``wait_for`` enforced: the run's
    captured hour, not the one-minute default a reload installed since."""
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), default_timeout=60)
    manager._run_inner = AsyncMock(side_effect=asyncio.TimeoutError)
    manager._claim_finalize = MagicMock(return_value=False)
    manager._teardown_run_session = AsyncMock()
    manager._release_slot = MagicMock(return_value=False)
    info = SubagentInfo(id="captured-hour", task="test", timeout_secs=3600)

    with patch("kiro_crew.subagent.Stats"):
        await manager._run(info)

    assert info.timeout_secs == 3600
    assert info.error.startswith("Timed out after 60 minutes")


@pytest.mark.asyncio
async def test_legacy_duck_provider_stream_keeps_one_argument():
    calls: list[str] = []
    sessions = _mock_sessions(lambda *_args, **_kwargs: None)
    provider = sessions._provider

    async def legacy_stream(message: str):
        calls.append(message)
        yield _complete_event()

    provider.stream = legacy_stream
    mgr = _manager(sessions)

    info = await _spawn_and_wait(mgr)

    assert info.error == ""
    assert calls == ["built_message"]


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
    timeouts: list[float | None] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)
        timeouts.append(kw.get("timeout"))

        async def _gen():
            if len(calls) <= 1 + TRANSIENT_RETRIES:
                raise _TransientError("backend throttle")
            yield _text_event("fb result")
            yield _complete_event()

        return _gen()

    sessions = _mock_sessions(stream_factory)
    provider = _TimeoutCapableProvider(stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions._provider = provider
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
    assert timeouts[0] is not None
    assert timeouts == [timeouts[0]] * len(calls)
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
    test_llm_helpers_fallback_restore.py)."""
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


# ── 1b. Provider throttle with a durable task queue open ─────────────

_THROTTLE_TEXT = (
    "Bedrock is throttling requests. Try: (1) wait a few seconds and retry, or (2) "
    "switch to a different model in the picker (e.g. sonnet)."
)


def _throttle_first_prompt() -> tuple[list[str], object]:
    """A backend that answers the first prompt with a throttle, before any text or
    tool call, and the second prompt normally."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                raise AcpError(_THROTTLE_TEXT, transient=True)
            yield _text_event("finished after the throttle")
            yield _complete_event()

        return _gen()

    return calls, stream_factory


async def _queued_manager(stream_factory) -> tuple[SubagentManager, list[object]]:
    """A manager with its durable task queue open, recording each dependency park."""
    mgr = _manager(_mock_sessions(stream_factory))
    await wait_taskq_open(mgr)
    assert await mgr.dependency_coordinator_async() is not None, "premise: a coordinator"
    parks: list[object] = []
    real_yield = mgr._yield_for_dependency

    async def _spy(info, signal):
        outcome = await real_yield(info, signal)
        parks.append(outcome)
        return outcome

    mgr._yield_for_dependency = _spy
    return mgr, parks


async def _spawn_with_queue(mgr: SubagentManager, **spawn_kw) -> SubagentInfo:
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0),
    ):
        info = mgr.spawn("do work", **spawn_kw)
        assert info is not None
        await await_until(mgr._tasks[info.id], what="the run finishing")
    return mgr._agents.get(info.id, info)


@pytest.mark.asyncio
async def test_throttle_on_a_run_with_no_task_row_retries_in_turn():
    """A run whose memory mode keeps no durable row cannot wait on the dependency
    coordinator (its wait has nowhere to be recorded), so the throttle takes the
    in-turn ladder instead of failing the run. The throttle answered the prompt
    before any text or tool call, so the same prompt is sent again."""
    calls, factory = _throttle_first_prompt()
    mgr, parks = await _queued_manager(factory)

    info = await _spawn_with_queue(mgr, _memory_mode="temporary")

    assert len(parks) == 1, "the coordinator was not consulted"
    assert info.error == "", info.error
    assert "finished after the throttle" in info.result
    assert calls == ["built_message", "built_message"]


@pytest.mark.asyncio
async def test_throttle_on_a_run_with_a_task_row_waits_on_the_coordinator():
    """A persistent run still parks on the coordinator's schedule and resumes from it."""
    calls, factory = _throttle_first_prompt()
    mgr, parks = await _queued_manager(factory)

    info = await _spawn_with_queue(mgr)

    assert parks == [True], parks
    assert info.error == "", info.error
    assert "finished after the throttle" in info.result
    assert calls == ["built_message", "built_message"]


@pytest.mark.asyncio
async def test_a_coordinator_give_up_still_fails_the_run():
    """When the coordinator ends the scope (its deadline or attempts cap), the row is
    already failed, so the run fails with the throttle and is not retried in turn."""
    from kiro_crew.taskq.dependency import OUTCOME_DEADLINE, Verdict

    calls, factory = _throttle_first_prompt()
    mgr, parks = await _queued_manager(factory)

    async def _gave_up(self, coordinator, info, signal):
        return Verdict(
            outcome=OUTCOME_DEADLINE,
            state="failed",
            scope=signal.dependency_scope,
            attempts=5,
            reason=f"dependency {signal.dependency_scope} unavailable after 5 attempts",
        )

    with patch.object(type(mgr._run_events), "_dependency_verdict", _gave_up):
        info = await _spawn_with_queue(mgr)

    assert parks == [False], parks
    assert "throttling" in info.error
    assert calls == ["built_message"]  # no in-turn retry


@pytest.mark.asyncio
async def test_a_throttle_on_a_row_that_already_ended_stops_the_run():
    """The run's row is cancelled under it (a Stop that landed while the prompt was
    out) before the throttle arrives. The coordinator's wait writes carry the run's
    generation, so they are refused and the row is found ended: the run stops with
    the error and never retries work that is not its own any more."""
    calls: list[str] = []
    mgr_box: dict[str, SubagentManager] = {}

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                mgr = mgr_box["mgr"]
                (run_id,) = [i for i, a in mgr._agents.items() if not a.done]
                assert mgr._taskq.cancel(run_id, reason="stopped by the user") is not None
                raise AcpError(_THROTTLE_TEXT, transient=True)
            yield _text_event("finished after the throttle")
            yield _complete_event()

        return _gen()

    mgr, parks = await _queued_manager(stream_factory)
    mgr_box["mgr"] = mgr

    info = await _spawn_with_queue(mgr)

    assert parks == [False], parks
    assert "throttling" in info.error
    assert calls == ["built_message"]  # no in-turn retry
    assert mgr._taskq.state_of(info.id) == "cancelled"
