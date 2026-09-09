"""A user-stopped monitor record must be CLEARABLE, or its session is dead.

``monitor_stop`` retains its outcome, and ``_stopped_row_is_replaceable``
refuses to let a re-arm displace a consumer-recorded stop. That rule is
deliberate. What was missing is the way out it names -- *its owner must clear it
first* -- because none of the three routes did it:

* ``monitor_watch`` / ``add_monitor``: refused, which is the rule working.
* ``DELETE /api/autonudge/{loop_id}``: routed a structured monitor into
  ``stop_monitor``, which returns an already-terminal loop UNCHANGED, then
  answered ``{"ok": true}``. The dashboard's own button reported success and
  removed nothing.
* ``POST /api/monitors/{id}/restart``: revives the SAME monitor, so it can only
  ever re-watch the subject the user stopped watching.

Net effect: a session that stopped a watch on one pull request could never
watch another one. These tests pin the fix and keep the rule it must not break.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import autonudge_authz
from kiro_crew.autonudge import AutoNudgeService, MonitorUpdateConflict
from kiro_crew.dashboard.handlers import autonudge as h
from kiro_crew.monitoring.models import (
    MONITOR_STATE_VERSION,
    MonitorBudgets,
    MonitorOutcome,
)


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def sel_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the audit sink in BOTH modules that write to it."""
    sink = MagicMock()
    monkeypatch.setattr(autonudge_authz, "sel", lambda: sink)
    monkeypatch.setattr(h, "sel", lambda: sink)
    return sink


@pytest.fixture
def svc(tmp_path: Any) -> AutoNudgeService:
    return AutoNudgeService(base_dir=tmp_path)


SLOT = "chat-1-123"
PR_ONE = "owner/repo#123"
PR_TWO = "owner/repo#456"


async def _arm(svc: AutoNudgeService, target: str, *, directive: bool = False) -> Any:
    """Arm a monitor on the slot.

    ``directive=True`` is the ``monitor_watch`` shape: create-only, opting into
    displacing a SYSTEM-imposed stop (``replace_stopped``) and nothing else. That
    is the call the retained-evidence rule refuses, so it is the one the
    deadlock test has to make.
    """
    return await svc.add_monitor(
        slot_key=SLOT,
        kind="github_pull_request",
        target=target,
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        replace_existing=not directive,
        replace_stopped=directive,
    )


async def _clear(svc: AutoNudgeService, loop_id: str) -> tuple[bool, str | None, int]:
    return await autonudge_authz.authorize_and_clear_monitor(
        svc=svc,
        loop_id=loop_id,
        session_key=SLOT,
        source="dashboard",
    )


# --- the deadlock itself ------------------------------------------------------


@pytest.mark.asyncio
async def test_clearing_a_user_stopped_monitor_frees_the_slot(svc: AutoNudgeService) -> None:
    """The whole bug in one test: stop, refused re-arm, clear, re-arm elsewhere."""
    armed = await _arm(svc, PR_ONE)
    stopped = await svc.stop_monitor(armed.id)
    assert stopped is not None and stopped.monitor is not None
    assert stopped.monitor.outcome is MonitorOutcome.USER_STOP

    # The rule that makes the clear necessary, asserted here so a change to it
    # cannot quietly make this test pass for the wrong reason.
    with pytest.raises(MonitorUpdateConflict, match="retained as evidence"):
        await _arm(svc, PR_TWO, directive=True)

    cleared, error, status = await _clear(svc, armed.id)
    assert (cleared, error, status) == (True, None, 200)
    assert svc.get_by_slot(SLOT) is None
    assert svc.get_by_id(armed.id) is None

    fresh = await _arm(svc, PR_TWO, directive=True)
    assert fresh.monitor is not None and fresh.monitor.target == PR_TWO
    assert svc.get_by_slot(SLOT) is fresh


@pytest.mark.asyncio
async def test_clear_is_audited_as_its_own_critical_operation(
    svc: AutoNudgeService, sel_mock: MagicMock
) -> None:
    """A removal that leaves no audit record is how evidence disappears."""
    armed = await _arm(svc, PR_ONE)
    await svc.stop_monitor(armed.id)
    sel_mock.reset_mock()

    await _clear(svc, armed.id)

    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["tool_name"] == "monitor_clear"
    assert kwargs["outcome"] == "invoked"
    assert kwargs["critical"] is True
    assert kwargs["session_key"] == SLOT
    assert kwargs["metadata"]["loop_id"] == armed.id


@pytest.mark.asyncio
async def test_clear_refuses_when_the_audit_sink_is_unavailable(
    svc: AutoNudgeService, sel_mock: MagicMock
) -> None:
    armed = await _arm(svc, PR_ONE)
    await svc.stop_monitor(armed.id)
    sel_mock.log_tool_invocation.side_effect = RuntimeError("sink down")

    cleared, error, status = await _clear(svc, armed.id)

    assert cleared is False and status == 503
    assert error == "audit log unavailable — monitor not cleared"
    assert svc.get_by_id(armed.id) is not None


# --- what clearing must NOT reach --------------------------------------------


@pytest.mark.asyncio
async def test_clear_refuses_a_live_monitor(svc: AutoNudgeService) -> None:
    """Clearing a RUNNING watch would delete it with no evidence it existed.

    Stopping is the only way to end a live monitor, because that is the path
    that writes the record.
    """
    armed = await _arm(svc, PR_ONE)

    cleared, error, status = await _clear(svc, armed.id)

    assert cleared is False and status == 409
    assert error == "only a stopped monitor can be cleared"
    assert svc.get_by_id(armed.id) is armed


@pytest.mark.asyncio
async def test_clear_refuses_a_record_from_a_newer_gateway(svc: AutoNudgeService) -> None:
    """Same rule the arm path applies: a downgrade may not delete what it cannot read."""
    armed = await _arm(svc, PR_ONE)
    await svc.stop_monitor(armed.id)
    stored = svc.get_by_id(armed.id)
    assert stored is not None and stored.monitor is not None
    stored.monitor.version = MONITOR_STATE_VERSION + 1

    cleared, error, status = await _clear(svc, armed.id)

    assert cleared is False and status == 409
    assert error is not None and "newer gateway" in error
    assert svc.get_by_id(armed.id) is not None


@pytest.mark.asyncio
async def test_clear_refuses_while_a_wake_is_in_flight(svc: AutoNudgeService) -> None:
    """A terminal record can still own an accepted wake; its completion needs the row."""
    armed = await _arm(svc, PR_ONE)
    await svc.stop_monitor(armed.id)
    stored = svc.get_by_id(armed.id)
    assert stored is not None and stored.monitor is not None
    stored.monitor.wake_in_flight = True

    cleared, error, status = await _clear(svc, armed.id)

    assert cleared is False and status == 409
    assert error == "monitor cannot be cleared while a wake is in flight"
    assert svc.get_by_id(armed.id) is not None


@pytest.mark.asyncio
async def test_clear_of_an_unknown_id_is_a_refusal_not_a_silent_ok(
    svc: AutoNudgeService,
) -> None:
    cleared, error, status = await _clear(svc, "mon-gone")
    assert (cleared, error, status) == (False, "structured monitor not found", 404)


# --- the HTTP route ----------------------------------------------------------


def _delete_request(loop_id: str) -> web.Request:
    """A DELETE from the configured dashboard owner."""
    app = web.Application()
    app["state"] = MagicMock(owner_id="U_OWNER")
    request = make_mocked_request(
        "DELETE",
        f"/api/autonudge/{loop_id}",
        app=app,
        match_info={"loop_id": loop_id},
    )
    request["user"] = "U_OWNER"
    request["app"] = ""
    return request


def _body(response: web.StreamResponse) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


@pytest.mark.asyncio
async def test_delete_route_clears_a_stopped_monitor_instead_of_reporting_a_no_op(
    svc: AutoNudgeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard button's own path: it must actually remove the row."""
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)
    armed = await _arm(svc, PR_ONE)
    await svc.stop_monitor(armed.id)

    response = await h.api_autonudge_delete(_delete_request(armed.id))

    # Row first, response shape second: the defect was a truthful-looking body
    # over an unchanged store, so the store is what this test is about.
    assert svc.get_by_slot(SLOT) is None
    assert svc.get_by_id(armed.id) is None
    assert response.status == 200
    assert _body(response) == {"ok": True, "cleared": True}


@pytest.mark.asyncio
async def test_delete_route_still_stops_a_live_monitor_and_keeps_the_record(
    svc: AutoNudgeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retained-evidence behaviour is unchanged for a running watch."""
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)
    armed = await _arm(svc, PR_ONE)

    response = await h.api_autonudge_delete(_delete_request(armed.id))

    assert response.status == 200
    retained = svc.get_by_id(armed.id)
    assert retained is not None and retained.monitor is not None
    assert retained.monitor.outcome is MonitorOutcome.USER_STOP
