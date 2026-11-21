"""The applied outcome of a loop directive reaches the agent that asked for it.

The monitor tools answer "requested" over their own pipe and the directive is applied
only after the turn's result is processed, so the agent cannot read the outcome from
the tool. A Bot's chat and a Bot's worker session never replay a ``notice`` row to the
model, so there the outcome is queued as an ``[Automation notice]`` that drains as the
session's next turn. Every session gets a notice when an arm, revision or stop fails,
whether it was refused or broke.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.dashboard import session_directive_apply as sda
from kiro_crew.dashboard.chat_turn.steer_queue import _QUEUE_KIND_ACTORS
from kiro_crew.dashboard.chat_utils import AUTOMATION_NOTICE_KIND, is_system_injection_item
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.members import forget_crewmate_worker_session, note_crewmate_worker_session

WORKER_SLOT = "chat-worker-1"


@pytest.fixture
def worker_session():
    note_crewmate_worker_session(WORKER_SLOT, "mem-builder")
    yield WORKER_SLOT
    forget_crewmate_worker_session(WORKER_SLOT)


def _slot(key: str, queued: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        _app="",
        messages=[],
        queue_append=lambda content, *a, **k: queued.append({"content": content, **k}) or "q1",
    )


def _assert_queued_as_a_turn(entry: dict) -> None:
    # A system-injection kind drains as the session's next turn and is never taken
    # for the person stepping in.
    assert entry["content"].startswith("[Automation notice] ")
    assert entry["kind"] == AUTOMATION_NOTICE_KIND
    assert is_system_injection_item({"kind": entry["kind"], "content": entry["content"]})
    assert _QUEUE_KIND_ACTORS[AUTOMATION_NOTICE_KIND] == "autonudge"


@pytest.mark.asyncio
async def test_a_refused_arm_in_a_worker_session_is_queued_for_the_worker(worker_session):
    queued: list[dict] = []
    surfaced = MagicMock()
    refused = AsyncMock(side_effect=sda._DirectiveDenied("this binding cannot host a loop"))
    with (
        patch("kiro_crew.dashboard.session_directive_apply._monitor_start", refused),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
        patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            _slot(worker_session, queued),
            f"dashboard:{worker_session}",
            "monitor_start",
            {"message": "watch the PR", "idle_secs": 600},
            producer_is_user_facing=True,
        )

    assert "cannot host a loop" in result
    surfaced.assert_called_once()
    assert len(queued) == 1
    _assert_queued_as_a_turn(queued[0])
    assert sda.ARM_REFUSAL_NOTICE_PREFIX in queued[0]["content"]
    assert "cannot host a loop" in queued[0]["content"]


@pytest.mark.asyncio
async def test_an_arm_that_breaks_is_reported_as_not_armed(worker_session):
    queued: list[dict] = []
    surfaced = MagicMock()
    broken = AsyncMock(side_effect=RuntimeError("scheduler unavailable"))
    with (
        patch("kiro_crew.dashboard.session_directive_apply._monitor_start", broken),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
        patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            _slot(worker_session, queued),
            f"dashboard:{worker_session}",
            "monitor_start",
            {"message": "watch the PR", "idle_secs": 600},
            producer_is_user_facing=True,
        )

    assert result.startswith("Error applying monitor_start")
    surfaced.assert_called_once()
    assert surfaced.call_args.args[3].startswith(sda.ARM_REFUSAL_NOTICE_PREFIX)
    assert len(queued) == 1
    _assert_queued_as_a_turn(queued[0])
    assert "scheduler unavailable" in queued[0]["content"]


@pytest.mark.asyncio
async def test_a_stop_in_a_worker_session_reports_stopped_to_the_worker(tmp_path, worker_session):
    service = AutoNudgeService(base_dir=tmp_path)
    await service.add(slot_key=worker_session, message="Watch it.", idle_secs=300)
    queued: list[dict] = []
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                _slot(worker_session, queued),
                f"dashboard:{worker_session}",
                "monitor_stop",
                {"reason": "done"},
            )
    finally:
        service.stop()

    assert "stopped" in result
    assert service.get_by_slot(worker_session) is None
    assert len(queued) == 1
    _assert_queued_as_a_turn(queued[0])
    assert sda.STOP_OUTCOME_NOTICE_PREFIX in queued[0]["content"]
    assert result in queued[0]["content"]


@pytest.mark.asyncio
async def test_a_wake_delivered_stop_outcome_keeps_the_wakes_provenance(tmp_path, worker_session):
    # The queued outcome drains as the worker's next turn. A stop that turn retries
    # must still meet the stale-wake guard, so the originating wake's identity rides
    # on the entry, as it does for a refused stop.
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add(slot_key=worker_session, message="Watch it.", idle_secs=300)
    queued: list[dict] = []
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
        ):
            await apply_session_directive(
                SimpleNamespace(),
                _slot(worker_session, queued),
                f"dashboard:{worker_session}",
                "monitor_stop",
                {"reason": "done"},
                producer_is_self_wake=True,
                producer_wake_loop_id=loop.id,
            )
    finally:
        service.stop()

    assert len(queued) == 1
    _assert_queued_as_a_turn(queued[0])
    assert queued[0]["meta"] == {
        sda.REFUSAL_SELF_WAKE_META_KEY: True,
        sda.REFUSAL_WAKE_LOOP_ID_META_KEY: loop.id,
    }


@pytest.mark.asyncio
async def test_a_stop_with_no_loop_tells_the_worker_nothing_was_stopped(tmp_path, worker_session):
    service = AutoNudgeService(base_dir=tmp_path)
    queued: list[dict] = []
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                _slot(worker_session, queued),
                f"dashboard:{worker_session}",
                "monitor_stop",
                {"reason": "done"},
            )
    finally:
        service.stop()

    assert "nothing to stop" in result
    assert len(queued) == 1
    _assert_queued_as_a_turn(queued[0])
    assert "nothing to stop" in queued[0]["content"]


@pytest.mark.asyncio
async def test_a_plain_session_stop_queues_nothing(tmp_path):
    # A plain chat's transcript is read by the person who drives it, so only the
    # sessions that cannot read their notices are woken.
    service = AutoNudgeService(base_dir=tmp_path)
    await service.add(slot_key="chat-1", message="Watch it.", idle_secs=300)
    queued: list[dict] = []
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=service),
            patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(),
                _slot("chat-1", queued),
                "dashboard:chat-1",
                "monitor_stop",
                {"reason": "done"},
            )
    finally:
        service.stop()

    assert "stopped" in result
    assert queued == []
