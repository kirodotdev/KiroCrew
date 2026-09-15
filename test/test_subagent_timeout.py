from __future__ import annotations

import json

import pytest

import kiro_crew.subagent_timeout as timeout
from conftest import make_dir_link


@pytest.fixture
def timeout_state(tmp_path, monkeypatch):
    path = tmp_path / "member-memory-bindings" / "subagent-timeout.json"
    monkeypatch.setattr(timeout, "_timeout_state_path", lambda: path)
    return path


def test_state_location_is_gateway_only(tmp_path, monkeypatch):
    from kiro_crew import sandbox, security

    monkeypatch.setattr(timeout, "config_dir", lambda: tmp_path)

    path = timeout._timeout_state_path()
    assert path == tmp_path / "member-memory-bindings" / "subagent-timeout.json"
    assert "subagent-timeout" not in sandbox._CREW_HIDDEN_LEAVES
    assert "subagent-timeout" not in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert "member-memory-bindings" in sandbox._CREW_READONLY_LEAVES
    assert "member-memory-bindings" in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert security.is_sensitive_path("~/.kiro/crew/member-memory-bindings/subagent-timeout.json")


def test_timeout_raises_future_deadline_by_thirty_minutes(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)

    adjustment = policy.observe(1800, 1800, completed=False)

    assert adjustment.timeout_secs == 3600
    assert adjustment.reason == "timeout"
    assert not timeout_state.exists()


def test_near_limit_completion_raises_proactively(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)

    adjustment = policy.observe(1800, 1500, completed=True)

    assert adjustment.timeout_secs == 3600
    assert adjustment.reason == "near_limit_completion"


def test_ordinary_completion_does_not_raise(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)

    adjustment = policy.observe(1800, 600, completed=True)

    assert adjustment.timeout_secs == 1800
    assert not adjustment.changed
    assert not timeout_state.exists()


def test_concurrent_old_deadline_observation_does_not_raise_twice(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)

    first = policy.observe(1800, 1800, completed=False)
    second = policy.observe(1800, 1800, completed=False)

    assert first.timeout_secs == 3600
    assert second.timeout_secs == 3600
    assert not second.changed


def test_growth_stops_at_ceiling(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)

    assert policy.observe(1800, 1800, completed=False).timeout_secs == 3600
    assert policy.observe(3600, 3600, completed=False).timeout_secs == 5400
    assert policy.observe(5400, 5400, completed=False).timeout_secs == 7200
    assert policy.observe(7200, 7200, completed=False).timeout_secs == 7200


def test_state_round_trip(timeout_state):
    timeout.write_learned_timeout(5400)

    assert timeout.read_learned_timeout() == 5400
    assert json.loads(timeout_state.read_text(encoding="utf-8")) == {"timeout_secs": 5400}


def test_unprotected_legacy_state_is_ignored(timeout_state):
    legacy = timeout_state.parent.parent / "subagent-timeout" / "timeout_state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"timeout_secs": 7200}\n', encoding="utf-8")

    assert timeout.read_learned_timeout() is None


def test_failed_atomic_write_preserves_last_learned_timeout(timeout_state, monkeypatch):
    timeout.write_learned_timeout(5400)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(timeout, "atomic_write", fail_write)
    timeout.write_learned_timeout(7200)

    assert timeout.read_learned_timeout() == 5400


def test_state_round_trip_when_agent_reads_are_fenced(timeout_state, monkeypatch):
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "is_sensitive_path", lambda *_args: True)

    timeout.write_learned_timeout(5400)

    assert hooks.is_sensitive_path(str(timeout_state))
    assert timeout.read_learned_timeout() == 5400


def test_state_read_refuses_redirected_parent(timeout_state):
    outside = timeout_state.parent.parent / "outside"
    outside.mkdir()
    (outside / timeout_state.name).write_text('{"timeout_secs": 7200}\n', encoding="utf-8")
    make_dir_link(timeout_state.parent, outside)

    assert timeout.read_learned_timeout() is None


def test_state_read_refuses_oversized_record(timeout_state):
    timeout_state.parent.mkdir(parents=True)
    timeout_state.write_bytes(b" " * (timeout._STATE_MAX_BYTES + 1))

    assert timeout.read_learned_timeout() is None


def test_state_write_replaces_hardlink_without_touching_target(timeout_state):
    target = timeout_state.parent.parent / "security_policy.json"
    target.write_text("ceiling", encoding="utf-8")
    timeout_state.parent.mkdir(parents=True)
    timeout_state.hardlink_to(target)

    timeout.write_learned_timeout(3600)

    assert target.read_text(encoding="utf-8") == "ceiling"
    assert timeout.read_learned_timeout() == 3600


def test_state_write_refuses_redirected_parent(timeout_state):
    outside = timeout_state.parent.parent / "outside"
    outside.mkdir()
    make_dir_link(timeout_state.parent, outside)

    timeout.write_learned_timeout(3600)

    assert not (outside / timeout_state.name).exists()


def test_state_read_rejects_hardlink_alias(timeout_state):
    target = timeout_state.parent.parent / "security_policy.json"
    target.write_text('{"timeout_secs": 7200}\n', encoding="utf-8")
    timeout_state.parent.mkdir(parents=True)
    timeout_state.hardlink_to(target)

    assert timeout.read_learned_timeout() is None


def test_policy_restore_honors_manual_floor_and_ceiling(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(9000, 7200, enabled=True)

    assert policy.restore(12000) == 9000
    assert policy.max_secs == 9000


def test_disabled_policy_ignores_history_and_observations(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=False)

    assert policy.restore(7200) == 1800
    adjustment = policy.observe(1800, 1800, completed=False)
    assert adjustment.timeout_secs == 1800
    assert not adjustment.changed


@pytest.mark.parametrize(
    ("stalled", "stall_suspect_at"),
    [(True, 0.0), (False, 1.0)],
)
def test_no_activity_timeout_does_not_train_manager(
    timeout_state,
    stalled,
    stall_suspect_at,
):
    import time
    from unittest.mock import MagicMock, patch

    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=60,
        adaptive_timeout=True,
        max_timeout=3600,
    )
    info = SubagentInfo(
        id="stalled-timeout",
        task="test",
        timeout_secs=60,
        stalled=stalled,
        _stall_suspect_at=stall_suspect_at,
    )
    info._exec_started = time.time() - 60

    with patch.object(manager, "_schedule_timeout_persist") as persist:
        learned = manager._observe_timeout_usage(info, completed=False)

    assert learned == 60
    assert manager._default_timeout == 60
    persist.assert_not_called()


def test_near_limit_success_still_trains_after_stall_signal(timeout_state):
    import time
    from unittest.mock import MagicMock, patch

    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=60,
        adaptive_timeout=True,
        max_timeout=3600,
    )
    info = SubagentInfo(
        id="recovered-near-limit",
        task="test",
        timeout_secs=60,
        stalled=True,
    )
    info._exec_started = time.time() - 50

    with patch.object(manager, "_schedule_timeout_persist") as persist:
        learned = manager._observe_timeout_usage(info, completed=True)

    assert learned == 1860
    persist.assert_called_once_with(1860)


def test_corrupt_and_invalid_state_is_ignored(timeout_state):
    timeout_state.parent.mkdir(parents=True)
    timeout_state.write_bytes(b'{"timeout_secs": true}\n')
    assert timeout.read_learned_timeout() is None

    timeout_state.write_bytes(b"not json\n")
    assert timeout.read_learned_timeout() is None


def test_delayed_restore_never_lowers_an_early_in_memory_increase(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)
    assert policy.observe(1800, 1800, completed=False).timeout_secs == 3600

    assert policy.restore(1800) == 3600


def test_policy_reconfigure_preserves_learned_level_within_new_bounds(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)
    assert policy.restore(5400) == 5400

    assert policy.reconfigure(1200, 7200, enabled=True) == 5400
    assert policy.reconfigure(1200, 3600, enabled=True) == 3600
    assert policy.reconfigure(6000, 3600, enabled=True) == 6000


def test_policy_reconfigure_disable_and_reenable_requires_restore(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)
    assert policy.restore(5400) == 5400

    assert policy.reconfigure(1800, 7200, enabled=False) == 1800
    assert policy.reconfigure(1800, 7200, enabled=True) == 1800
    assert policy.restore(5400) == 5400


def test_policy_construction_does_no_file_io(monkeypatch):
    monkeypatch.setattr(
        timeout,
        "read_learned_timeout",
        lambda: (_ for _ in ()).throw(AssertionError("constructor read state")),
    )

    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)

    assert policy.current_secs == 1800


@pytest.mark.asyncio
async def test_manager_restores_learned_timeout_off_loop(timeout_state):
    from unittest.mock import MagicMock

    from kiro_crew.subagent import SubagentManager

    timeout.write_learned_timeout(5400)
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=1800,
        adaptive_timeout=True,
        max_timeout=7200,
    )

    assert manager._default_timeout == 1800
    await manager._load_timeout_history()
    assert manager._default_timeout == 5400


@pytest.mark.asyncio
async def test_first_run_restores_history_before_capturing_deadline(timeout_state):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentInfo, SubagentManager

    timeout.write_learned_timeout(5400)
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=1800,
        adaptive_timeout=True,
        max_timeout=7200,
    )
    manager._run_inner = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._teardown_run_session = AsyncMock()
    manager._release_slot = MagicMock(return_value=False)
    info = SubagentInfo(id="restored-first-run", task="test")

    await manager._run(info)

    assert manager._timeout_history_loaded
    assert info.timeout_secs == 5400
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_config_preserves_learned_level_and_restores_on_opt_in(timeout_state):
    from unittest.mock import MagicMock, patch

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.subagent import SubagentManager

    timeout.write_learned_timeout(5400)
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=1800,
        adaptive_timeout=False,
        max_timeout=7200,
    )
    fresh = KiroCrewConfig()
    fresh.agent.subagent_timeout_secs = 1800
    fresh.agent.subagent_timeout_auto = True
    fresh.agent.subagent_timeout_max_secs = 7200

    with patch("kiro_crew.subagent.resolve_max_subagents", return_value=3):
        await manager.reconfigure(fresh)
    assert manager._default_timeout == 5400

    fresh.agent.subagent_timeout_secs = 1200
    manager.apply_limits(fresh, max_concurrent=3)
    assert manager._default_timeout == 5400


@pytest.mark.asyncio
async def test_run_timeout_raises_future_manager_deadline(timeout_state):
    import asyncio
    import time
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=60,
        adaptive_timeout=True,
        max_timeout=3600,
    )
    manager._run_inner = AsyncMock(side_effect=asyncio.TimeoutError)
    manager._claim_finalize = MagicMock(return_value=False)
    manager._teardown_run_session = AsyncMock()
    manager._release_slot = MagicMock(return_value=False)
    info = SubagentInfo(id="adaptive-timeout", task="test")
    info._exec_started = time.time() - 1
    info.last_activity = time.time()

    with (
        patch("kiro_crew.subagent.Stats"),
        patch.object(manager, "_schedule_timeout_persist") as persist,
    ):
        await manager._run(info)

    assert info.timeout_secs == 60
    assert manager._default_timeout == 1860
    assert "future runs use 31 minutes" in info.error
    persist.assert_called_once_with(1860)


@pytest.mark.asyncio
async def test_near_limit_success_raises_future_manager_deadline(timeout_state):
    import time
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=60,
        adaptive_timeout=True,
        max_timeout=3600,
    )
    info = SubagentInfo(id="adaptive-success", task="test")

    async def complete(_info, _session_key):
        _info._exec_started = time.time() - 50
        _info.done = True

    manager._run_inner = complete
    manager._claim_finalize = MagicMock(return_value=False)
    manager._teardown_run_session = AsyncMock()
    manager._release_slot = MagicMock(return_value=False)

    with patch.object(manager, "_schedule_timeout_persist") as persist:
        await manager._run(info)

    assert manager._default_timeout == 1860
    persist.assert_called_once_with(1860)


@pytest.mark.asyncio
async def test_silent_run_timeout_does_not_raise_future_manager_deadline(timeout_state):
    import asyncio
    import time
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=60,
        adaptive_timeout=True,
        max_timeout=3600,
    )
    manager._run_inner = AsyncMock(side_effect=asyncio.TimeoutError)
    manager._claim_finalize = MagicMock(return_value=False)
    manager._teardown_run_session = AsyncMock()
    manager._release_slot = MagicMock(return_value=False)
    info = SubagentInfo(id="silent-adaptive-timeout", task="test")
    info._exec_started = time.time() - 60
    info.last_activity = info._exec_started

    with (
        patch("kiro_crew.subagent.Stats"),
        patch.object(manager, "_schedule_timeout_persist") as persist,
    ):
        await manager._run(info)

    assert info.timeout_secs == 60
    assert manager._default_timeout == 60
    assert "future runs use" not in info.error
    persist.assert_not_called()


@pytest.mark.parametrize(
    (
        "exec_elapsed",
        "user_stopped",
        "expected_reap",
        "expected_timeout",
        "persistence_calls",
    ),
    [
        (61, False, True, 1860, 1),
        (61, True, True, 60, 0),
        (None, False, False, 60, 0),
        (10, False, False, 60, 0),
    ],
)
@pytest.mark.asyncio
async def test_reaper_uses_execution_elapsed_and_learns_only_from_execution_timeouts(
    timeout_state,
    exec_elapsed,
    user_stopped,
    expected_reap,
    expected_timeout,
    persistence_calls,
):
    import asyncio
    import time
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=60,
        adaptive_timeout=True,
        max_timeout=3600,
    )
    manager._timeout_history_loaded = True
    manager._conv_registry_rebuilt = True
    now = time.time()
    info = SubagentInfo(
        id="adaptive-reaper",
        task="test",
        started=now - 61,
        timeout_secs=60,
        _pid=123,
    )
    info._exec_started = None if exec_elapsed is None else now - exec_elapsed
    manager._agents[info.id] = info

    async def force_reap_side_effect(*_args):
        info.user_stopped = user_stopped

    with (
        patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        patch("kiro_crew.subagent.compact_cost_log"),
        patch("kiro_crew.subagent.prune_stale_tombstones", return_value=0),
        patch.object(manager, "_schedule_timeout_persist") as persist,
        patch.object(manager, "_sample_live_costs"),
        patch.object(manager, "_sweep_stuck_waves"),
        patch.object(manager, "_sweep_digest_holds"),
        patch.object(manager, "_sweep_conversations"),
        patch.object(manager, "_maybe_flag_stall", new_callable=AsyncMock),
        patch.object(
            manager,
            "_force_reap",
            new_callable=AsyncMock,
            side_effect=force_reap_side_effect,
        ) as force_reap,
    ):
        with pytest.raises(asyncio.CancelledError):
            await manager._reaper_loop()

    if expected_reap:
        force_reap.assert_awaited_once_with(info.id, info, pytest.approx(exec_elapsed, abs=1))
    else:
        force_reap.assert_not_awaited()
    assert manager._default_timeout == expected_timeout
    assert persist.call_count == persistence_calls


def test_timeout_controls_are_dashboard_editable():
    from kiro_crew.constants import SUBAGENT_TIMEOUT_MAX, SUBAGENT_TIMEOUT_MIN
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    assert _EDITABLE_CONFIG["agent.subagent_timeout_secs"] == {
        "type": "int",
        "min": SUBAGENT_TIMEOUT_MIN,
        "max": SUBAGENT_TIMEOUT_MAX,
    }
    assert _EDITABLE_CONFIG["agent.subagent_timeout_auto"] == {"type": "bool"}
    assert _EDITABLE_CONFIG["agent.subagent_timeout_max_secs"]["max"] == 86400


@pytest.mark.asyncio
async def test_cancel_all_drains_in_flight_learned_timeout_write(timeout_state):
    """A restart racing the persist write must not discard the learned level.

    Reproduces the sub-second window the reviewer flagged: a qualifying run
    raises the level in memory and schedules the detached write, then the
    process is torn down (``cancel_all`` -> reexec) before the write lands. The
    write is manager-owned and drained by ``cancel_all``, so the raised level is
    on disk before the reexec and a fresh manager restores it — instead of
    reading back the stale lower floor.
    """
    import asyncio
    import time
    from unittest.mock import MagicMock

    from kiro_crew.subagent import SubagentInfo, SubagentManager

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=1800,
        adaptive_timeout=True,
        max_timeout=7200,
    )

    # Hold the write open so it is provably in-flight (scheduled, not durable)
    # when cancel_all runs, rather than completing in the gap before shutdown.
    release = asyncio.Event()

    async def _gated_persist(timeout_secs: int) -> None:
        await release.wait()
        timeout.write_learned_timeout(timeout_secs)

    manager._persist_timeout_level = _gated_persist

    info = SubagentInfo(id="raise-then-restart", task="test", timeout_secs=1800)
    info._exec_started = time.time() - 1800

    raised = manager._observe_timeout_usage(info, completed=False)
    assert raised == 3600
    assert manager._timeout_persist_tasks  # scheduled, not yet durable
    assert timeout.read_learned_timeout() is None

    # Unblock the write, then shut down. With no runs/reports pending, the only
    # await inside cancel_all is the persist drain, so the write reaches disk
    # *because* cancel_all drains it — before the reexec that follows.
    release.set()
    await manager.cancel_all()

    assert timeout.read_learned_timeout() == 3600
    assert not manager._timeout_persist_tasks

    # A fresh process restores the raised level, not the stale floor.
    revived = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        default_timeout=1800,
        adaptive_timeout=True,
        max_timeout=7200,
    )
    await revived._load_timeout_history()
    assert revived._default_timeout == 3600
