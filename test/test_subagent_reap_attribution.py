"""A run that WE stop must report the stop, not the death the stop caused.

``_force_reap`` tears a dedicated run's session down FIRST (``sessions.reset`` ->
``provider.shutdown()`` -> ``runtime.kill(reason="provider shutdown")``) and only
then cancels the run task. The run's in-flight ``client.stream`` sees the poisoned
queue before that cancel lands and raises ``AcpProcessDied`` -- "Runtime process
died during prompt -- killed (provider shutdown)". An ``except Exception`` arm that
takes that echo at face value stores the death text as ``info.error``, writes a
``cause="error"`` tombstone and logs ``Subagent X failed`` at ERROR with a full
traceback -- for a run the user has just pressed Stop on, or that a parent end has
deliberately cancelled -- and sends every reader of it to the provider, the OOM
killer and the leak reaper in turn.

These tests pin the attribution: a reap in flight (``_reap_started``) makes the
death an ECHO. The record names WHO stopped the run, the tombstone carries the
reap's own cause, a user/parent stop stays neutral (no ``error``), and nothing is
logged at ERROR.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import create_agent_folder

_DEATH_TEXT = (
    "Runtime process died during prompt — killed (provider shutdown) "
    "[returncode=<not reaped>] stderr_tail: <none>"
)


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    return tmp_path


def _manager() -> SubagentManager:
    sessions = MagicMock()
    sessions.release = MagicMock()
    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())
    # Spy on the terminal record: a delivered run's tombstone is later rewritten
    # to ``delivered``, so the cause the RECORD was written with is read from the
    # call, while the file on disk still carries the detail text.
    real = manager._write_tombstone
    manager._write_tombstone = MagicMock(side_effect=real)  # type: ignore[method-assign]
    return manager


def _record_causes(manager: SubagentManager) -> list[str]:
    return [c.args[1] for c in manager._write_tombstone.call_args_list]  # type: ignore[attr-defined]


def _arm_run(
    manager: SubagentManager, agent_id: str, parent: str
) -> tuple[SubagentInfo, asyncio.Event]:
    """Register a dedicated-process run whose stream dies once the reap resets it."""
    info = SubagentInfo(id=agent_id, task="t", parent_session_key=parent)
    info._session_sharing = False
    create_agent_folder(agent_id, task="t", parent_session=parent)
    manager._agents[agent_id] = info
    manager._running_count = 1
    died = asyncio.Event()

    async def _run_inner(_info, _session_key):
        info.streaming_text = "partial answer so far"
        await died.wait()
        raise AcpProcessDied(_DEATH_TEXT)

    async def _reset(_session_key, *_a, **_kw):
        # The reap is suspended in the session teardown while the run's stream
        # observes the kill and raises -- the ordering the field reports show.
        died.set()
        await asyncio.sleep(0.05)

    manager._sessions.reset = _reset
    manager._run_inner = _run_inner  # type: ignore[method-assign]
    return info, died


def _tombstone(agent_root, agent_id: str) -> dict:
    return json.loads((agent_root / agent_id / "tombstone.json").read_text(encoding="utf-8"))


def _patches(manager: SubagentManager):
    return (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent_manager.terminal.sel", create=True),
        patch.object(manager, "_fire_event", new_callable=AsyncMock),
        patch.object(manager, "_on_done", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_a_user_stop_is_reported_as_a_stop_not_a_runtime_death(agent_root, caplog):
    manager = _manager()
    info, _died = _arm_run(manager, "stopecho1", "dashboard:p")
    p = _patches(manager)
    with p[0], p[1], p[2], p[3], p[4], caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
        run_task = asyncio.create_task(manager._run(info))
        await asyncio.sleep(0)
        assert await manager.cancel("stopecho1") is True
        await asyncio.wait_for(run_task, timeout=10)

    # Neutral record: the death was our own doing.
    assert info.outcome == "stopped"
    assert not info.error
    assert info.result == "partial answer so far", "the partial output must survive a stop"
    assert _record_causes(manager) == ["user_stop"], _record_causes(manager)
    tomb = _tombstone(agent_root, "stopecho1")
    assert "AcpProcessDied" not in tomb.get("detail", "")
    assert "provider shutdown" not in tomb.get("detail", "")
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]
    stopped = [
        r for r in caplog.records if "stopped" in r.getMessage() and "stopecho1" in r.getMessage()
    ]
    assert stopped, "the stop must still leave one attributable log line"
    assert all(r.levelno < logging.WARNING for r in stopped), "a user's own stop is not a warning"


@pytest.mark.asyncio
async def test_a_parent_end_names_the_verb_that_ended_the_run(agent_root, caplog):
    manager = _manager()
    info, _died = _arm_run(manager, "stopecho2", "dashboard:p")
    p = _patches(manager)
    with p[0], p[1], p[2], p[3], p[4], caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
        run_task = asyncio.create_task(manager._run(info))
        await asyncio.sleep(0)
        stopped = await manager.cancel_for_teardown(
            ["stopecho2"],
            parent_session_key="dashboard:p",
            verb="retire_kiro_identity_sessions",
        )
        assert stopped == 1
        await asyncio.wait_for(run_task, timeout=10)

    assert info.outcome == "stopped"
    assert not info.error
    assert _record_causes(manager) == ["parent_end"], _record_causes(manager)
    tomb = _tombstone(agent_root, "stopecho2")
    assert "AcpProcessDied" not in tomb.get("detail", "")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # A parent end destroys live work the user did not ask to lose: the record of
    # that action and the run's own stop line are both visible at the default
    # WARNING level, and both name the verb.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "parent-end teardown" in m and "retire_kiro_identity_sessions" in m for m in warnings
    ), warnings
    assert any(
        "stopecho2" in m and "retire_kiro_identity_sessions" in m for m in warnings
    ), warnings


@pytest.mark.asyncio
async def test_a_deadline_reap_names_the_deadline_not_the_death(agent_root, caplog):
    manager = _manager()
    info, _died = _arm_run(manager, "stopecho3", "dashboard:p")
    p = _patches(manager)
    with p[0], p[1], p[2], p[3], p[4], caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
        run_task = asyncio.create_task(manager._run(info))
        await asyncio.sleep(0)
        await manager._force_reap("stopecho3", info, elapsed=900.0, reason="reaped")
        await asyncio.wait_for(run_task, timeout=10)

    # A deadline reap IS a failure, but its own one: the error names the reap.
    assert info.outcome == "failed"
    assert info.error is not None
    assert "reaped" in info.error and "900" in info.error
    assert "AcpProcessDied" not in info.error
    assert _record_causes(manager) == ["reaped"], _record_causes(manager)
    tomb = _tombstone(agent_root, "stopecho3")
    assert "AcpProcessDied" not in tomb.get("detail", "")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        r.levelno == logging.WARNING
        and "stopecho3" in r.getMessage()
        and "reaped" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_an_unrelated_exception_under_a_reap_is_still_the_runs_own_failure(
    agent_root, caplog
):
    """Only the runtime death is the echo.

    A run can hit a genuine bug of its own in the same instant a reap starts.
    Classifying every exception under a reap as the echo would bury that bug
    under a neutral "stopped" record; the arm therefore matches the runtime
    death alone and every other exception keeps the visible traceback.
    """
    manager = _manager()
    info = SubagentInfo(id="stopecho4", task="t", parent_session_key="dashboard:p")
    info._session_sharing = False
    create_agent_folder("stopecho4", task="t", parent_session="dashboard:p")
    manager._agents["stopecho4"] = info
    manager._running_count = 1
    failed = asyncio.Event()

    async def _run_inner(_info, _session_key):
        await failed.wait()
        raise RuntimeError("a bug of the run's own")

    async def _reset(_session_key, *_a, **_kw):
        failed.set()
        await asyncio.sleep(0.05)

    manager._sessions.reset = _reset
    manager._run_inner = _run_inner  # type: ignore[method-assign]
    p = _patches(manager)
    with p[0], p[1], p[2], p[3], p[4], caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
        run_task = asyncio.create_task(manager._run(info))
        await asyncio.sleep(0)
        await manager._force_reap("stopecho4", info, elapsed=5.0, reason="reaped")
        await asyncio.wait_for(run_task, timeout=10)

    assert info.outcome == "failed"
    assert info.error == "RuntimeError: a bug of the run's own"
    assert _record_causes(manager) == ["error"], _record_causes(manager)
    errors = [
        r for r in caplog.records if r.levelno >= logging.ERROR and "stopecho4" in r.getMessage()
    ]
    assert errors and errors[0].exc_info is not None, "the run's own fault keeps its traceback"


@pytest.mark.asyncio
async def test_a_childless_parent_end_is_not_a_warning(caplog):
    """The audit line warns about discarded work; a parent end with none stays INFO."""
    manager = _manager()
    with caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
        stopped = await manager.cancel_for_teardown(
            (), parent_session_key="dashboard:empty", verb="remove"
        )
    assert stopped == 0
    lines = [r for r in caplog.records if "parent-end teardown" in r.getMessage()]
    assert lines and all(r.levelno == logging.INFO for r in lines), [
        (r.levelname, r.getMessage()) for r in lines
    ]


def test_a_stage_cancel_names_itself_before_the_reap():
    """A stage-boundary cancel is neither a user Stop nor a parent end."""
    manager = _manager()
    info = SubagentInfo(id="stage0001", task="t", parent_session_key="dashboard:p")
    info._stage_boundary_owner = "owner-7"
    manager._agents["stage0001"] = info
    revoked = manager._cancellation._revoke_boundary_owners_impl("dashboard:p", "owner-7")
    assert [i.id for i in revoked] == ["stage0001"]
    assert info.user_stopped is True
    assert info._reap_reason == "stage_cancel"
    assert info._stop_origin == "stage cancelled (owner-7)"


@pytest.mark.asyncio
async def test_the_first_stopper_keeps_the_attribution():
    """A later parent end or stage cancel must not rewrite who stopped the run."""
    manager = _manager()
    info = SubagentInfo(id="first0001", task="t", parent_session_key="dashboard:p")
    info._stage_boundary_owner = "owner-9"
    manager._agents["first0001"] = info
    # The user's Stop has begun and named itself; the reap is still in flight.
    info._reap_reason = "user_stop"
    info._stop_origin = "stopped by user"
    manager._cancellation._revoke_boundary_owners_impl("dashboard:p", "owner-9")
    assert (info._reap_reason, info._stop_origin) == ("user_stop", "stopped by user")
    with patch.object(manager, "cancel", new_callable=AsyncMock, return_value=True):
        await manager.cancel_for_teardown(
            ["first0001"], parent_session_key="dashboard:p", verb="destroy"
        )
    assert (info._reap_reason, info._stop_origin) == ("user_stop", "stopped by user")


@pytest.mark.asyncio
async def test_a_late_stop_does_not_turn_a_deadline_reap_neutral(agent_root, caplog):
    """The first stopper owns the outcome.

    A deadline reap has begun tearing the run down when the user presses Stop.
    ``cancel`` sets ``user_stopped`` on the same record; the run's stream then
    dies. Reading ``user_stopped`` alone would report a neutral stop and hide the
    deadline failure -- the record must stay the deadline's: failed, with its
    error, tombstoned ``reaped``.
    """
    manager = _manager()
    info, _died = _arm_run(manager, "stopecho5", "dashboard:p")
    p = _patches(manager)
    with p[0], p[1], p[2], p[3], p[4], caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
        run_task = asyncio.create_task(manager._run(info))
        await asyncio.sleep(0)
        # The deadline reap owns the teardown; a Stop lands while its reset awaits.
        original_reset = manager._sessions.reset
        late_stop_fired = False

        async def _reset_then_late_stop(session_key, *a, **kw):
            nonlocal late_stop_fired
            if not late_stop_fired:
                late_stop_fired = True
                info.user_stopped = True  # what cancel() writes on the same record
            await original_reset(session_key, *a, **kw)

        manager._sessions.reset = _reset_then_late_stop
        await manager._force_reap("stopecho5", info, elapsed=900.0, reason="reaped")
        await asyncio.wait_for(run_task, timeout=10)

    assert info.outcome == "failed"
    assert info.error and "reaped" in info.error
    assert _record_causes(manager) == ["reaped"], _record_causes(manager)
