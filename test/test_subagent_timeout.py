from __future__ import annotations

import json

import pytest

import kiro_crew.subagent_timeout as timeout
from conftest import make_dir_link


@pytest.fixture
def timeout_state(tmp_path, monkeypatch):
    path = tmp_path / "subagents" / "timeout_state.json"
    monkeypatch.setattr(timeout, "_timeout_state_path", lambda: path)
    return path


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
    timeout.write_learned_timeout(5400, "timeout")

    assert timeout.read_learned_timeout() == 5400
    record = json.loads(timeout_state.read_text(encoding="utf-8"))
    assert record["reason"] == "timeout"


def test_state_write_replaces_hardlink_without_touching_target(timeout_state):
    target = timeout_state.parent.parent / "security_policy.json"
    target.write_text("ceiling", encoding="utf-8")
    timeout_state.parent.mkdir(parents=True)
    timeout_state.hardlink_to(target)

    timeout.write_learned_timeout(3600, "timeout")

    assert target.read_text(encoding="utf-8") == "ceiling"
    assert timeout.read_learned_timeout() == 3600


def test_state_write_refuses_redirected_parent(timeout_state):
    outside = timeout_state.parent.parent / "outside"
    outside.mkdir()
    make_dir_link(timeout_state.parent, outside)

    timeout.write_learned_timeout(3600, "timeout")

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


def test_corrupt_and_invalid_state_is_ignored(timeout_state):
    timeout_state.parent.mkdir(parents=True)
    timeout_state.write_text('{"timeout_secs": true}\n', encoding="utf-8")
    assert timeout.read_learned_timeout() is None

    timeout_state.write_text("not json\n", encoding="utf-8")
    assert timeout.read_learned_timeout() is None


def test_delayed_restore_never_lowers_an_early_in_memory_increase(timeout_state):
    policy = timeout.AdaptiveTimeoutPolicy(1800, 7200, enabled=True)
    assert policy.observe(1800, 1800, completed=False).timeout_secs == 3600

    assert policy.restore(1800) == 3600


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

    timeout.write_learned_timeout(5400, "timeout")
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
async def test_run_timeout_raises_future_manager_deadline(timeout_state):
    import asyncio
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

    with (
        patch("kiro_crew.subagent.Stats"),
        patch(
            "kiro_crew.subagent._safe_fire",
            side_effect=lambda coro: coro.close(),
        ) as safe_fire,
    ):
        await manager._run(info)

    assert info.timeout_secs == 60
    assert manager._default_timeout == 1860
    assert "future runs use 31 minutes" in info.error
    safe_fire.assert_called_once()


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

    with patch(
        "kiro_crew.subagent._safe_fire",
        side_effect=lambda coro: coro.close(),
    ) as safe_fire:
        await manager._run(info)

    assert manager._default_timeout == 1860
    safe_fire.assert_called_once()
