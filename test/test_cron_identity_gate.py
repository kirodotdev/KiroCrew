"""A cron fire runs the Kiro account-identity gate before it acquires its session.

A cron fire whose sub-agents are still running skips its session reset, and the
next fire of a ``persistent_session`` job, or of the same ``agent_sequence`` step,
reuses that live session. After an account switch or a ``kiro-cli logout`` outside
the dashboard its child still holds the previous account's credential. The
dashboard retires such a child with a per-turn gate,
``chat_runner._retire_sessions_on_identity_change``, run before ``get_or_create``.
The cron path ran no such gate, so on a host where only crons run an agent cron
kept answering on the previous account. These tests drive the real cron callback
through the real gate, over a fake prerequisite service and the gateway's own
session manager, and pin the order: gate first, then acquire. They also pin what
an unidentifiable account costs a cron fire: nothing. An empty fingerprint that
does not prove a sign-out (a social login the reader cannot identify) is never
reconciled and spares nothing, so a fire that swept on it would retire every idle
session every time; it touches no session instead. A proven sign-out still
sweeps, once: after that sweep a fire leaves it alone, so a job that brings its
own key keeps its session and sub-agents for as long as nobody signs in.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from member_memory_helpers import write_member_home

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.cron import CronJob, CronSchedule

_REPLY = "Agent response here"


class _Prerequisites:
    """The ``KiroPrerequisiteService`` surface the identity gate reads."""

    def __init__(
        self,
        *,
        changed: bool,
        live: str,
        absence_definitive: bool = False,
        already_swept: bool = False,
    ) -> None:
        self._changed = changed
        self._live = live
        self.identity_absence_is_definitive = absence_definitive
        self.identity_absence_already_swept = already_swept
        self.identity_absence_run = 1
        self.identity_observation_generation = 0
        self.reconciled: list[str] = []
        self.absence_swept: list[int] = []

    async def identity_changed_since_sessions(self) -> tuple[bool, str]:
        return self._changed, self._live

    def note_sessions_reconciled(
        self, fingerprint: str, *, observations_before: int | None = None
    ) -> None:
        self.reconciled.append(fingerprint)

    def note_absence_swept(self, run: int) -> None:
        self.absence_swept.append(run)

    def mark_signed_out(self) -> None:
        pass


def _record_gate_and_acquire(sessions: MagicMock, order: list[str], acquire_error=None) -> None:
    """Wire the gate's session surface and ``get_or_create`` to log their calls in order."""

    async def _flag(live: str) -> list[str]:
        order.append("flag")
        return []

    async def _retire(fingerprint: str = "") -> tuple[list[str], bool]:
        order.append(f"retire:{fingerprint}")
        return [], True

    async def _acquire(key: str, **_kwargs):
        order.append(f"acquire:{key}")
        if acquire_error is not None:
            raise acquire_error
        return MagicMock(), True, False

    sessions.flag_identity_stamp_mismatches = AsyncMock(side_effect=_flag)
    sessions.retire_kiro_identity_sessions = AsyncMock(side_effect=_retire)
    sessions.pending_identity_sweep_fingerprint = ""
    sessions.identity_sweep_waiting_on = ()
    sessions.get_or_create = AsyncMock(side_effect=_acquire)


def _dashboard_state(sessions: MagicMock, prerequisites: _Prerequisites) -> MagicMock:
    state = MagicMock()
    state.get_slot = MagicMock(return_value=None)
    state.has_slot = MagicMock(return_value=False)
    state.notify = MagicMock()
    state.kiro_prerequisite_service = prerequisites
    # One session manager, as in production: _init_dashboard and _init_api_server
    # hand the gateway's own to the dashboard.
    state.sessions = sessions
    return state


def _make_gw(prerequisites: _Prerequisites, order: list[str]):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.slack = None
    gw.conv_log = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._running_script_ids = set()
    gw._no_crons = False
    gw.cron_svc = MagicMock()
    gw.cron_svc.remove_job_async = AsyncMock(return_value=True)
    gw._cfg = MagicMock()
    gw._cfg.agent.provider = "acp"
    gw._cfg.hooks = {}
    gw._approval_mode = None
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.sessions.get_channel = MagicMock(return_value=None)
    gw.ctx_builder.build_message = MagicMock(return_value=("full prompt", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    _record_gate_and_acquire(gw.sessions, order)
    gw.dashboard_state = _dashboard_state(gw.sessions, prerequisites)
    return gw


async def _run_cron(gw, job):
    """Run *job* through the real ``_cron_callback`` captured from ``_init_cron``."""
    captured_cb = None
    with (
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
        patch(
            "kiro_crew.slack.gateway.run_in_embed_pool",
            AsyncMock(return_value=("full prompt", None)),
        ),
        patch("kiro_crew.slack.gateway.stream_and_collect", AsyncMock(return_value=_REPLY)),
        patch("kiro_crew.slack.gateway.persist_token_record_async", AsyncMock()),
        patch("kiro_crew.slack.gateway.sel"),
        patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
    ):
        mock_ctx.return_value = (f"cron:{job.id}", job.message)

        def capture_cron(on_job=None, **_kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            svc.remove_job_async = AsyncMock(return_value=True)
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        assert captured_cb is not None
        return await captured_cb(job)


def _agent_job(job_id: str) -> CronJob:
    return CronJob(
        id=job_id,
        name="agent-cron",
        message="Run daily check",
        schedule=CronSchedule(kind="every", every_secs=3600),
    )


def _kiro_provider(session_key=None, agent=None, channel_id=None, **kwargs):
    """A provider whose child reads kiro-cli's identity store, as the sweep requires."""
    provider = MagicMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.memory_mode = kwargs.get("memory_mode", "persistent")
    provider.is_process_alive = lambda: True
    provider.has_active_turn = lambda: False
    provider.runtime_abort_target = lambda: None
    provider.uses_kiro_identity_store = True
    return provider


class _ChildTeardown:
    """``SubagentManager``'s two teardown halves: the runs each parent owns, and every cancel."""

    def __init__(self, owned: dict[str, tuple[str, ...]]) -> None:
        self._owned = owned
        self.cancelled: list[tuple[str, ...]] = []

    def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
        return self._owned.get(parent_session_key, ())

    async def cancel_for_teardown(
        self, agent_ids, *, parent_session_key: str = "", verb: str = ""
    ) -> int:
        self.cancelled.append(tuple(agent_ids))
        return len(self.cancelled[-1])


def _on_real_manager(gw, manager) -> list:
    """Run the gate's session surface and the cron's acquire on *manager*.

    Returns the list each provider the cron acquires is appended to.
    """
    gw.sessions.flag_identity_stamp_mismatches = manager.flag_identity_stamp_mismatches
    gw.sessions.retire_kiro_identity_sessions = manager.retire_kiro_identity_sessions
    gw.sessions.pending_identity_sweep_fingerprint = ""
    gw.sessions.identity_sweep_waiting_on = ()
    acquired = []

    async def _acquire(key: str, **kwargs):
        result = await manager.get_or_create(key, **kwargs)
        acquired.append(result[0])
        return result

    gw.sessions.get_or_create = AsyncMock(side_effect=_acquire)
    return acquired


@pytest.mark.asyncio
async def test_a_changed_account_retires_stale_sessions_before_the_cron_acquires() -> None:
    order: list[str] = []
    prerequisites = _Prerequisites(changed=True, live="fp-current")
    gw = _make_gw(prerequisites, order)

    result = await _run_cron(gw, _agent_job("gate"))

    # The sweep ran against the live account BEFORE the session was acquired,
    # so the stale child is gone when get_or_create looks for one.
    assert order == ["flag", "retire:fp-current", "acquire:cron:gate"]
    # A complete sweep reconciles the baseline, so the next fire does not re-sweep.
    assert prerequisites.reconciled == ["fp-current"]
    # The run itself still lands.
    assert _REPLY in result


@pytest.mark.asyncio
async def test_an_unchanged_account_acquires_without_a_sweep() -> None:
    order: list[str] = []
    prerequisites = _Prerequisites(changed=False, live="fp-current")
    gw = _make_gw(prerequisites, order)

    result = await _run_cron(gw, _agent_job("steady"))

    # Only the cheap per-turn stamp check runs; nothing is retired.
    assert order == ["flag", "acquire:cron:steady"]
    gw.sessions.retire_kiro_identity_sessions.assert_not_awaited()
    assert prerequisites.reconciled == []
    assert _REPLY in result


@pytest.mark.asyncio
async def test_a_sequence_step_runs_the_gate_before_its_acquire(monkeypatch) -> None:
    from kiro_crew.slack import gateway

    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    # Resolution reads ``live.snapshot() or self._cfg``; pin both to the test config
    # so the two crew aliases are visible without a live config watcher.
    monkeypatch.setattr(gateway.live, "snapshot", lambda: cfg)

    order: list[str] = []
    prerequisites = _Prerequisites(changed=True, live="fp-current")
    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.sessions = MagicMock()
    _record_gate_and_acquire(
        gw.sessions, order, acquire_error=RuntimeError("stop before the provider")
    )
    gw.ctx_builder = MagicMock()
    gw.slack = gw.conv_log = gw.subagent_mgr = None
    gw.dashboard_state = _dashboard_state(gw.sessions, prerequisites)
    gw._owner_id = "owner"
    gw._cron_injecting = {}
    gw._no_crons = False
    gw._cfg = cfg
    gw.cron_svc = None
    callbacks = []

    async def create(on_job=None, **_kwargs):
        callbacks.append(on_job)
        service = MagicMock()
        service.start = AsyncMock()
        return service

    monkeypatch.setattr(gateway.CronService, "create", create)
    monkeypatch.setattr(
        gateway, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False))
    )
    await gw._init_cron()

    job = CronJob(id="seq", name="sequence", message="task", agent_sequence=["beta", "alpha"])
    with pytest.raises(RuntimeError, match="stop before the provider"):
        await callbacks[0](job)

    # The step's own acquire, not only the single-agent one, is gated.
    assert order == ["flag", "retire:fp-current", "acquire:cron:seq:beta"]


@pytest.mark.asyncio
async def test_a_kept_alive_cron_session_from_the_old_account_is_retired_before_the_cron_acquires() -> (
    None
):
    from kiro_crew.session import SessionManager

    manager = SessionManager(KiroCrewConfig(), provider_factory=_kiro_provider)
    # The previous fire's session, kept alive past its run while sub-agents
    # finish: started before the account change, idle now.
    stale, _is_new, _resumed = await manager.get_or_create("cron:alive")
    manager.release("cron:alive")

    prerequisites = _Prerequisites(changed=True, live="fp-current")
    gw = _make_gw(prerequisites, [])
    # The gate's surface and the acquire run on the real manager, so the sweep
    # sees the live session and the acquire makes the real reuse decision.
    acquired = _on_real_manager(gw, manager)

    try:
        result = await _run_cron(gw, _agent_job("alive"))

        # Without the gate the fire reuses the live session on the old account.
        stale.shutdown.assert_awaited()
        assert len(acquired) == 1 and acquired[0] is not stale
        assert _REPLY in result
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_an_account_the_gate_cannot_identify_leaves_every_session_alone() -> None:
    from kiro_crew.session import SessionManager

    manager = SessionManager(KiroCrewConfig(), provider_factory=_kiro_provider)
    children = _ChildTeardown({"dashboard:parent": ("run-parent",), "cron:alive": ("run-cron",)})
    manager.set_child_teardown_handler(children)
    # An idle dashboard chat whose turn ended with spawn_run children running...
    parent, _is_new, _resumed = await manager.get_or_create("dashboard:parent")
    manager.release("dashboard:parent")
    # ...and the previous fire's session, kept alive while its own sub-agents finish.
    kept, _is_new, _resumed = await manager.get_or_create("cron:alive")
    manager.release("cron:alive")

    # Empty without proving a sign-out: a social login the reader cannot identify.
    prerequisites = _Prerequisites(changed=True, live="")
    gw = _make_gw(prerequisites, [])
    acquired = _on_real_manager(gw, manager)
    sweep = AsyncMock(side_effect=manager.retire_kiro_identity_sessions)
    gw.sessions.retire_kiro_identity_sessions = sweep

    try:
        result = await _run_cron(gw, _agent_job("alive"))

        # No sweep: under this fingerprint it would recur on every fire.
        sweep.assert_not_awaited()
        parent.shutdown.assert_not_awaited()
        assert manager.get_provider("dashboard:parent") is parent
        # The cron's own session goes on with its sub-agents: retiring it would
        # end them on every fire.
        kept.shutdown.assert_not_awaited()
        assert acquired == [kept]
        assert children.cancelled == []
        assert prerequisites.reconciled == []
        assert _REPLY in result
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_a_proven_sign_out_still_sweeps_before_the_cron_acquires() -> None:
    order: list[str] = []
    prerequisites = _Prerequisites(changed=True, live="", absence_definitive=True)
    gw = _make_gw(prerequisites, order)

    result = await _run_cron(gw, _agent_job("signed-out"))

    # Nobody is signed in, so every child still holding the old credential is
    # retired, as a dashboard turn would retire it; an empty fingerprint is never
    # reconciled, so the finished sweep is recorded against this sign-out instead.
    assert order == ["flag", "retire:", "acquire:cron:signed-out"]
    assert prerequisites.reconciled == []
    assert prerequisites.absence_swept == [1]
    assert _REPLY in result


@pytest.mark.asyncio
async def test_a_sign_out_an_earlier_sweep_finished_is_left_alone() -> None:
    order: list[str] = []
    prerequisites = _Prerequisites(
        changed=True, live="", absence_definitive=True, already_swept=True
    )
    gw = _make_gw(prerequisites, order)

    result = await _run_cron(gw, _agent_job("after-sign-out"))

    # Nothing that held the old account is left, so the fire acquires directly.
    assert order == ["acquire:cron:after-sign-out"]
    assert prerequisites.absence_swept == []
    assert _REPLY in result


@pytest.mark.asyncio
async def test_one_cron_fire_sweeps_a_sign_out_and_later_fires_leave_it_alone(
    tmp_path, sel_private_root
) -> None:
    from kiro_crew import kiro_prerequisite as kp
    from kiro_crew.session import SessionManager

    manager = SessionManager(KiroCrewConfig(), provider_factory=_kiro_provider)
    children = _ChildTeardown({"cron:keyed": ("run-keyed",)})
    manager.set_child_teardown_handler(children)
    # Nobody is signed in: no kiro-cli store, no API key, an empty Crew vault.
    service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
    gw = _make_gw(_Prerequisites(changed=True, live=""), [])
    gw.dashboard_state.kiro_prerequisite_service = service
    _on_real_manager(gw, manager)
    sweep = AsyncMock(side_effect=manager.retire_kiro_identity_sessions)
    gw.sessions.retire_kiro_identity_sessions = sweep

    try:
        await _run_cron(gw, _agent_job("first"))
        assert sweep.await_count == 1
        # A job that brings its own key, kept alive while its sub-agents run.
        keyed, _is_new, _resumed = await manager.get_or_create(
            "cron:keyed", extra_env={"KIRO_API_KEY": "job-key"}
        )
        manager.release("cron:keyed")

        result = await _run_cron(gw, _agent_job("second"))

        # Sweeping again would retire the keyed session and cancel its sub-agents
        # on every fire for as long as nobody signs in.
        assert sweep.await_count == 1
        keyed.shutdown.assert_not_awaited()
        assert children.cancelled == []
        assert _REPLY in result
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_a_changed_account_discards_a_pooled_old_account_provider() -> None:
    from kiro_crew.session import SessionManager

    manager = SessionManager(KiroCrewConfig(), provider_factory=_kiro_provider)
    # Warmed before the account change, so it holds the previous account.
    pooled = _kiro_provider()
    manager._warm_pool.put_nowait((pooled, 0.0))

    prerequisites = _Prerequisites(changed=True, live="fp-current")
    gw = _make_gw(prerequisites, [])
    acquired = _on_real_manager(gw, manager)

    try:
        result = await _run_cron(gw, _agent_job("pool"))

        # The fire's gate drains it, and a `cron:` key never claims a pooled process.
        pooled.shutdown.assert_awaited()
        assert len(acquired) == 1 and acquired[0] is not pooled
        assert _REPLY in result
    finally:
        await manager.close_all()
