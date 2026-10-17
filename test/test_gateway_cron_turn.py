"""The single-agent cron turn, pinned through ``on_job`` before it leaves ``gateway.py``.

``_init_cron`` builds one ``_cron_callback`` per gateway and hands it to the cron
service as ``on_job``. For a message job without an agent sequence that callback
registers the run's session key, acquires a session, builds the prompt, streams the
turn and delivers the result inside one ``try``; its ``except`` retries an ACP death
or a transient backend error by re-entering the whole callback, attributes a runtime
death, and alerts; its ``finally`` hands the post-compaction re-injection and the
skill-body dedup back, releases the session and resets it unless sub-agent work is
still pending.

These tests drive that callback through ``on_job`` with every collaborator replaced
and pin what an extraction must keep: the order the ``finally`` runs in, which flags
each exit hands to it, that a retry re-enters the whole callback, that a run is
charged at most once, and that the failure re-raised is the one that happened.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cron_gateway_integration_helpers import _make_gw_for_llm, _make_llm_job

from kiro_crew import execution_context, runtime_death
from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.cron_service.model import _AUTO_PAUSE_THRESHOLD
from kiro_crew.slack import gateway as gw_mod


class _Transient(RuntimeError):
    """A backend error ``acp_error_is_transient`` classifies as retryable."""

    transient = True


class _RecordingAsyncio:
    """The facade's ``asyncio`` with ``sleep`` recorded instead of waited on.

    ``raises``, when set, is raised from ``sleep`` after it is recorded.
    """

    def __init__(self, calls: list[Any]) -> None:
        self._calls = calls
        self.raises: BaseException | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, *_args: Any) -> None:
        self._calls.append(("sleep", delay))
        if self.raises is not None:
            raise self.raises


class _Fire:
    """One gateway with the cron callback captured and its effects recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[Any] = []
        self.pending = False
        self.on_job: Any = None
        self.stream_text = "Agent response here"
        self.stop_reason: str | None = "end_turn"
        self.stream_hook: Any = None
        self.svc = MagicMock(name="cron_svc")
        self.svc.register_active_session_key.side_effect = lambda *_a: self.calls.append("register")
        self.svc.clear_active_session_key.side_effect = lambda *_a: self.calls.append("clear")
        self.client = MagicMock(name="client")
        self.client.cwd = ""

        gw = _make_gw_for_llm()
        gw._no_crons = True
        gw.subagent_mgr = MagicMock(name="subagent_mgr")
        gw.sessions.get_or_create = AsyncMock(side_effect=self._get_or_create)
        gw.sessions.release = MagicMock(side_effect=lambda _key: self.calls.append("release"))
        gw.sessions.reset = AsyncMock(side_effect=lambda _key: self.calls.append("reset"))
        gw._deliver_cron_to_channel = AsyncMock(return_value=False)
        gw._deliver_failure_alert = AsyncMock(side_effect=self._alert)
        gw.dashboard_state.conversation_log = None
        self.gw = gw
        self.acquire_errors: list[BaseException] = []
        self.alert_error: BaseException | None = None

        def _capture(on_job: Any = None, **_kw: Any) -> MagicMock:
            self.on_job = on_job
            return self.svc

        monkeypatch.setattr(
            gw_mod, "CronService", SimpleNamespace(create=AsyncMock(side_effect=_capture))
        )
        monkeypatch.setattr(gw_mod, "sel", MagicMock(name="sel"))
        monkeypatch.setattr(
            gw_mod, "build_cron_session_context", lambda job: (f"cron:{job.id}", job.message)
        )
        monkeypatch.setattr(
            gw_mod, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False))
        )
        monkeypatch.setattr(gw_mod, "_pre_create_cron_slot", AsyncMock())
        monkeypatch.setattr(gw_mod, "_retire_sessions_on_identity_change", AsyncMock())
        monkeypatch.setattr(gw_mod, "publish_turn_identity", AsyncMock())
        monkeypatch.setattr(gw_mod, "persist_token_record_async", AsyncMock())
        monkeypatch.setattr(gw_mod, "inject_cron_result_to_dashboard", MagicMock())
        monkeypatch.setattr(gw_mod, "consume_reinjection", MagicMock(return_value=True))
        monkeypatch.setattr(gw_mod, "rearm_reinjection", self._rearm)
        monkeypatch.setattr(gw_mod, "rollback_skill_bodies", self._rollback)
        monkeypatch.setattr(gw_mod, "_subagent_work_pending", self._work_pending)
        monkeypatch.setattr(
            gw_mod, "run_in_embed_pool", AsyncMock(return_value=("full prompt", None))
        )
        monkeypatch.setattr(gw_mod, "stream_and_collect", self._stream)
        monkeypatch.setattr(gw_mod, "transient_retry_delay", lambda _attempt: 0.25)
        self.asyncio = _RecordingAsyncio(self.calls)
        monkeypatch.setattr(gw_mod, "asyncio", self.asyncio)
        monkeypatch.setattr(execution_context, "bind_session_execution", MagicMock())
        monkeypatch.setattr(gw_mod.live, "snapshot", lambda: None)
        runtime_death._reset_for_tests()

    async def _get_or_create(self, key: str, **_kw: Any) -> tuple[Any, bool, bool]:
        self.calls.append("acquire")
        if self.acquire_errors:
            raise self.acquire_errors.pop(0)
        return self.client, True, False

    async def _stream(self, client: Any, msg: str, **kw: Any) -> str:
        self.calls.append("stream")
        if self.stream_hook is not None:
            self.stream_hook(kw)
        if self.stop_reason is not None:
            kw["on_complete"](SimpleNamespace(stop_reason=self.stop_reason))
        return self.stream_text

    async def _alert(self, job: Any, **_kw: Any) -> tuple[bool, bool, bool]:
        self.calls.append("alert")
        if self.alert_error is not None:
            raise self.alert_error
        return False, True, False

    def _rearm(self, _sessions: Any, _key: str, **kw: Any) -> None:
        self.calls.append(("rearm", kw))

    def _rollback(self, _builder: Any, _key: str, **kw: Any) -> None:
        self.calls.append(("rollback", kw))

    async def _work_pending(self, _mgr: Any, _key: str) -> bool:
        self.calls.append("pending?")
        return self.pending

    async def fire(self, job: Any) -> Any:
        if self.on_job is None:
            await self.gw._init_cron()
        return await self.on_job(job)

    def names(self) -> list[str]:
        return [c if isinstance(c, str) else c[0] for c in self.calls]

    def rearms(self) -> list[dict[str, Any]]:
        return [c[1] for c in self.calls if not isinstance(c, str) and c[0] == "rearm"]


@pytest.fixture
def fire(monkeypatch: pytest.MonkeyPatch) -> _Fire:
    harness = _Fire(monkeypatch)
    yield harness
    runtime_death._reset_for_tests()


# ── the finally ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_landed_turn_hands_back_then_releases_then_resets(fire: _Fire) -> None:
    result = await fire.fire(_make_llm_job())

    assert result == "Agent response here"
    assert fire.names() == [
        "register",
        "acquire",
        "stream",
        "rearm",
        "rollback",
        "release",
        "pending?",
        "reset",
        "clear",
    ]
    assert fire.rearms() == [{"consumed": True, "landed": True, "compacted": False}]
    assert ("rollback", {"landed": True}) in fire.calls


@pytest.mark.asyncio
async def test_pending_sub_agent_work_defers_the_reset_and_keeps_the_registration(
    fire: _Fire,
) -> None:
    fire.pending = True

    result = await fire.fire(_make_llm_job())

    assert result == "Agent response here"
    assert fire.names()[-3:] == ["rollback", "release", "pending?"]
    assert "reset" not in fire.names()
    assert "clear" not in fire.names()


@pytest.mark.asyncio
async def test_a_compaction_reported_mid_stream_reaches_the_finally(fire: _Fire) -> None:
    fire.stream_hook = lambda kw: kw["on_compaction"](SimpleNamespace(text="completed"))

    await fire.fire(_make_llm_job())

    assert fire.rearms() == [{"consumed": True, "landed": True, "compacted": True}]


@pytest.mark.asyncio
async def test_two_fires_do_not_share_the_compaction_box(fire: _Fire) -> None:
    fire.stream_hook = lambda kw: kw["on_compaction"](SimpleNamespace(text="completed"))
    await fire.fire(_make_llm_job())
    fire.stream_hook = None

    await fire.fire(_make_llm_job())

    assert [r["compacted"] for r in fire.rearms()] == [True, False]


@pytest.mark.asyncio
async def test_a_turn_without_a_completion_is_handed_back_unlanded(fire: _Fire) -> None:
    fire.stop_reason = None

    await fire.fire(_make_llm_job())

    assert fire.rearms() == [{"consumed": True, "landed": False, "compacted": False}]
    assert ("rollback", {"landed": False}) in fire.calls


@pytest.mark.asyncio
async def test_a_raise_after_the_reinjection_is_consumed_hands_it_back(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    boom = RuntimeError("context assembly failed")
    monkeypatch.setattr(gw_mod, "run_in_embed_pool", AsyncMock(side_effect=boom))
    job = _make_llm_job()

    with pytest.raises(RuntimeError) as raised:
        await fire.fire(job)

    assert raised.value is boom
    assert "stream" not in fire.names()
    assert fire.rearms() == [{"consumed": True, "landed": False, "compacted": False}]
    assert fire.names()[-7:] == [
        "alert",
        "rearm",
        "rollback",
        "release",
        "pending?",
        "reset",
        "clear",
    ]
    assert job.consecutive_failures == 1


@pytest.mark.asyncio
async def test_a_raise_before_the_acquire_releases_nothing(fire: _Fire) -> None:
    boom = RuntimeError("session store unreadable")
    fire.acquire_errors = [boom]

    with pytest.raises(RuntimeError) as raised:
        await fire.fire(_make_llm_job())

    assert raised.value is boom
    assert fire.rearms() == [{"consumed": False, "landed": False, "compacted": False}]
    assert "release" not in fire.names()
    assert "reset" not in fire.names()


@pytest.mark.asyncio
async def test_a_raise_in_the_delivery_tail_after_a_landed_turn_keeps_it_landed(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    boom = RuntimeError("bell unavailable")
    fire.gw.dashboard_state.notify = MagicMock(side_effect=boom)

    with pytest.raises(RuntimeError) as raised:
        await fire.fire(_make_llm_job())

    assert raised.value is boom
    assert fire.rearms() == [{"consumed": True, "landed": True, "compacted": False}]


# ── one charge per run ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_all_blocked_run_whose_delivery_then_fails_is_charged_once(
    fire: _Fire,
) -> None:
    def _refuse_every_call(kw: dict[str, Any]) -> None:
        kw["on_tool_gate"]("Running: @fs/write_file", False, True)

    fire.stream_hook = _refuse_every_call
    fire.gw.dashboard_state.notify = MagicMock(side_effect=RuntimeError("bell unavailable"))
    job = _make_llm_job()

    with pytest.raises(RuntimeError):
        await fire.fire(job)

    assert job.consecutive_failures == 1
    assert job.last_status == "error"


@pytest.mark.asyncio
async def test_a_cancellation_during_the_failure_alert_leaves_the_counters(
    fire: _Fire,
) -> None:
    fire.acquire_errors = [RuntimeError("spawn refused")]
    fire.alert_error = asyncio.CancelledError()
    job = _make_llm_job()

    with pytest.raises(asyncio.CancelledError):
        await fire.fire(job)

    assert job.consecutive_failures == 0
    assert job.last_failure_hash == ""
    # The finally still ran on the way out.
    assert fire.rearms() == [{"consumed": False, "landed": False, "compacted": False}]


@pytest.mark.asyncio
async def test_a_duplicate_failure_reraises_the_same_exception_and_counts(
    fire: _Fire, caplog: pytest.LogCaptureFixture
) -> None:
    job = _make_llm_job()
    fire.acquire_errors = [RuntimeError("spawn refused")]
    with pytest.raises(RuntimeError):
        await fire.fire(job)
    assert job.last_failure_hash
    second = RuntimeError("spawn refused")
    fire.acquire_errors = [second]
    caplog.clear()

    with caplog.at_level(logging.ERROR, logger="kiro_crew.slack.gateway"):
        with pytest.raises(RuntimeError) as raised:
            await fire.fire(job)

    assert raised.value is second
    assert fire.names().count("alert") == 1
    assert job.consecutive_failures == 2
    logged = [r for r in caplog.records if r.getMessage() == "Cron job 'llm-job' failed"]
    assert len(logged) == 1
    assert logged[0].exc_info is not None and logged[0].exc_info[1] is second


# ── retries re-enter the whole callback ─────────────────────────────────────


@pytest.mark.asyncio
async def test_an_acp_death_before_dispatch_retries_through_the_whole_callback(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = AsyncMock(return_value=(None, False))
    monkeypatch.setattr(gw_mod, "_await_cron_fire_time_gate", gate)
    fire.acquire_errors = [AcpProcessDied("ACP process pipe broken: Connection lost")]
    job = _make_llm_job()

    result = await fire.fire(job)

    assert result == "Agent response here"
    assert gate.await_count == 2
    names = fire.names()
    # The retry's own finally (rearm .. reset) runs before this frame's.
    assert names == [
        "register",
        "acquire",
        "reset",
        "register",
        "acquire",
        "stream",
        "rearm",
        "rollback",
        "release",
        "pending?",
        "reset",
        "clear",
        "rearm",
        "rollback",
    ]
    assert fire.rearms()[-1] == {"consumed": False, "landed": False, "compacted": False}
    assert getattr(job, "_acp_retried", None) is False


@pytest.mark.asyncio
async def test_a_failing_acp_retry_alerts_once_with_the_original_error(fire: _Fire) -> None:
    first = AcpProcessDied("ACP process pipe broken: Connection lost")
    fire.acquire_errors = [first, RuntimeError("still broken")]
    job = _make_llm_job()

    with pytest.raises(AcpProcessDied) as raised:
        await fire.fire(job)

    assert raised.value is first
    assert fire.names().count("alert") == 1
    assert job.consecutive_failures == 1
    assert job._acp_retried is False


@pytest.mark.asyncio
async def test_a_transient_error_before_dispatch_hands_back_before_the_backoff(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = AsyncMock(return_value=(None, False))
    monkeypatch.setattr(gw_mod, "_await_cron_fire_time_gate", gate)
    monkeypatch.setattr(
        gw_mod,
        "run_in_embed_pool",
        AsyncMock(side_effect=[_Transient("throttled"), ("full prompt", None)]),
    )
    job = _make_llm_job()

    result = await fire.fire(job)

    assert result == "Agent response here"
    # The retry re-enters the whole callback: the fire-time gate runs again.
    assert gate.await_count == 2
    names = fire.names()
    first_sleep = names.index("sleep")
    assert names[: first_sleep + 1] == [
        "register",
        "acquire",
        "release",
        "rearm",
        "rollback",
        "sleep",
    ]
    assert fire.calls[first_sleep] == ("sleep", 0.25)
    assert fire.rearms()[0] == {"consumed": True, "landed": False}
    assert job._transient_attempts == 1
    # This frame handed its bookkeeping to the retry and released before the
    # backoff, so its own finally re-arms nothing and releases nothing.
    assert fire.rearms()[-1] == {"consumed": False, "landed": False, "compacted": False}
    assert names.count("release") == 2


@pytest.mark.asyncio
async def test_a_cancellation_in_the_backoff_does_not_release_twice(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gw_mod, "run_in_embed_pool", AsyncMock(side_effect=_Transient("throttled")))
    fire.asyncio.raises = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await fire.fire(_make_llm_job())

    assert fire.names() == [
        "register",
        "acquire",
        "release",
        "rearm",
        "rollback",
        "sleep",
        "rearm",
        "rollback",
    ]
    assert fire.rearms()[-1] == {"consumed": False, "landed": False, "compacted": False}


# ── whose fault a runtime death was ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_shared_runtime_death_is_not_charged_and_counts_the_job_streak(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    def _not_ours(provider: Any) -> bool:
        seen.append(provider)
        return False

    monkeypatch.setattr(runtime_death, "caused_by_this_session", _not_ours)
    fire.stream_hook = lambda _kw: (_ for _ in ()).throw(RuntimeError("runtime died"))
    job = _make_llm_job()

    with pytest.raises(RuntimeError):
        await fire.fire(job)

    assert seen == [fire.client]
    assert job.consecutive_failures == 0
    assert runtime_death.shared_deaths(f"cron:{job.id}") == 1


@pytest.mark.asyncio
async def test_a_shared_streak_at_the_threshold_hands_over_and_pauses(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_death, "caused_by_this_session", lambda _p: False)
    fire.stream_hook = lambda _kw: (_ for _ in ()).throw(RuntimeError("runtime died"))
    job = _make_llm_job()
    for _ in range(_AUTO_PAUSE_THRESHOLD - 1):
        runtime_death.note_shared_death(f"cron:{job.id}")

    with pytest.raises(RuntimeError):
        await fire.fire(job)

    assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD
    assert job.auto_paused is True
    assert runtime_death.shared_deaths(f"cron:{job.id}") == 0


# ── stall attribution reads the frame name ───────────────────────────────────


@pytest.mark.asyncio
async def test_the_stream_runs_under_a_frame_named_cron_callback(fire: _Fire) -> None:
    names: list[str] = []

    def _walk(_kw: dict[str, Any]) -> None:
        frame = sys._getframe()
        while frame is not None:
            names.append(frame.f_code.co_name)
            frame = frame.f_back

    fire.stream_hook = _walk

    await fire.fire(_make_llm_job())

    assert "_cron_callback" in names


# ── where the message arm starts ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_fire_time_gate_then_the_first_run_tab_precede_the_turn(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _gate(*_a: Any, **_kw: Any) -> tuple[None, bool]:
        fire.calls.append("gate")
        return None, False

    async def _tab(*_a: Any) -> None:
        fire.calls.append("tab")

    monkeypatch.setattr(gw_mod, "_await_cron_fire_time_gate", _gate)
    monkeypatch.setattr(gw_mod, "_pre_create_cron_slot", _tab)

    await fire.fire(_make_llm_job())

    assert fire.names()[:4] == ["gate", "tab", "register", "acquire"]


@pytest.mark.asyncio
async def test_a_denied_message_job_dispatches_nothing_and_counts_nothing(
    fire: _Fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gw_mod, "_await_cron_fire_time_gate", AsyncMock(return_value=("cron capability off", False))
    )
    job = _make_llm_job()

    assert await fire.fire(job) is None

    # The denial is alerted, and nothing is registered, acquired or streamed.
    assert fire.names() == ["alert"]
    assert fire.gw._deliver_failure_alert.await_args.kwargs["actor_key"] == "cron:lj1"
    assert job.fire_time_denied is True
    assert job.consecutive_failures == 0


# ── the agent sequence ───────────────────────────────────────────────────────


def _sequence_job() -> Any:
    return _make_llm_job(agent_sequence=["alpha", "beta"])


@pytest.mark.asyncio
async def test_each_sequence_step_hands_back_releases_and_resets_its_own_key(
    fire: _Fire,
) -> None:
    keys: list[str] = []
    fire.svc.register_active_session_key.side_effect = lambda _id, key: (
        keys.append(key),
        fire.calls.append("register"),
    )

    result = await fire.fire(_sequence_job())

    assert result == "Agent response here"
    step = [
        "register",
        "acquire",
        "stream",
        "rearm",
        "rollback",
        "release",
        "pending?",
        "reset",
        "clear",
    ]
    assert fire.names() == step + step
    assert keys == ["cron:lj1:alpha", "cron:lj1:beta"]


@pytest.mark.asyncio
async def test_pending_work_defers_a_steps_reset(fire: _Fire) -> None:
    fire.pending = True

    await fire.fire(_sequence_job())

    assert "reset" not in fire.names()
    assert fire.names().count("release") == 2


@pytest.mark.asyncio
async def test_one_retained_step_defers_the_whole_sequence_before_any_dispatch(
    fire: _Fire,
) -> None:
    fire.gw.sessions._get_session_agent = lambda key: "old-agent" if key.endswith(":beta") else ""
    job = _sequence_job()

    assert await fire.fire(job) is None

    assert fire.names() == []
    assert job.run_never_started is True


@pytest.mark.asyncio
async def test_a_failing_step_propagates_without_an_alert_or_a_charge(fire: _Fire) -> None:
    boom = RuntimeError("step failed")
    fire.stream_hook = lambda _kw: (_ for _ in ()).throw(boom)
    job = _sequence_job()

    with pytest.raises(RuntimeError) as raised:
        await fire.fire(job)

    assert raised.value is boom
    assert "alert" not in fire.names()
    assert job.consecutive_failures == 0
    assert fire.names()[-7:] == [
        "stream",
        "rearm",
        "rollback",
        "release",
        "pending?",
        "reset",
        "clear",
    ]
