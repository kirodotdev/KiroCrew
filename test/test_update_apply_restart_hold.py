"""``POST /api/update`` refuses to start an installer under an approved restart.

``kirocrew restart --when-idle`` asks the restart gate, which counts a dashboard
update only once its installer is registered. An update still copying memory
stores when the gate approves is not counted, so without this refusal its
installer would start and the approved restart would stop it partway through.
Both launches (the policy apply and the git ``dashboard-update`` worker) check the
gate's hold right before they start, with no await between.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _FakeProc,
    _git_proj,
    _isolated_module_state,
    _request,
    _sequence_procs,
)

from kiro_crew.dashboard.handlers import updates, updates_apply
from kiro_crew.slack.gateway_runtime import update_schedule

_UPSTREAM_OID = b"0123456789abcdef0123456789abcdef01234567"


def _approve_during_copy(monkeypatch, held: dict[str, bool]) -> None:
    """A memory copy during which the restart gate approves a restart."""

    async def _copy(_state):
        held["live"] = True
        return None

    monkeypatch.setattr(updates, "_snapshot_memory_or_refuse", _copy)


def _refused(resp) -> bool:
    return resp.status == 409 and json.loads(resp.body.decode())["code"] == "restart_pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "not-approved"])
async def test_a_policy_update_waits_out_a_restart_approved_during_its_copy(monkeypatch, approved):
    from kiro_crew.platform import update_provider

    held = {"live": False}
    monkeypatch.setattr(
        update_provider, "resolve_provider", lambda: SimpleNamespace(can_apply=lambda: True)
    )
    if approved:
        _approve_during_copy(monkeypatch, held)
    else:
        monkeypatch.setattr(updates, "_snapshot_memory_or_refuse", AsyncMock(return_value=None))
    applied = AsyncMock(return_value=True)
    monkeypatch.setattr(update_provider, "apply_policy_update", applied)
    monkeypatch.setattr(updates, "_restart_gateway", AsyncMock(return_value=True))
    req = _request({})
    req.app["state"]._restart_gate_held = lambda: held["live"]

    resp = await updates.api_update_apply(req)

    assert _refused(resp) is approved
    assert applied.await_count == (0 if approved else 1)
    assert not updates_apply._policy_apply_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "not-approved"])
async def test_a_git_update_waits_out_a_restart_approved_during_its_copy(
    monkeypatch, tmp_path, approved
):
    """The git path's own copy runs after the policy check, so it needs its own."""
    from kiro_crew import dep_sync

    held = {"live": False}
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
    monkeypatch.setattr(updates, "resolve_remote_url", lambda _p, **_k: "")
    monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
    monkeypatch.setattr(dep_sync, "incoming_python_floor_breach", lambda *a, **k: None)
    if approved:
        _approve_during_copy(monkeypatch, held)
    else:
        monkeypatch.setattr(updates, "_snapshot_memory_or_refuse", AsyncMock(return_value=None))
    # Clean tree, the diverged guard's fetch, a fast-forwardable count and the
    # upstream pin: the handler reaches the launch. A launched worker fails its
    # merge at once, so nothing past it runs.
    _sequence_procs(
        monkeypatch,
        [
            _FakeProc(out=b""),
            _FakeProc(out=b""),
            _FakeProc(out=b"0\t1\n"),
            _FakeProc(out=_UPSTREAM_OID + b"\n"),
            _FakeProc(returncode=1),
        ],
    )
    req = _request({})
    state = req.app["state"]
    state._restart_gate_held = lambda: held["live"]

    resp = await updates.api_update_apply(req)

    assert _refused(resp) is approved
    workers = list(state._background_tasks)
    assert len(workers) == (0 if approved else 1)
    if workers:
        done, pending = await asyncio.wait(workers, timeout=30)
        assert not pending


@pytest.mark.asyncio
async def test_the_hold_reads_live_only_while_the_approved_restart_is_pending():
    loop = asyncio.get_running_loop()
    gateway = SimpleNamespace(_restart_gate_hold=None)
    assert update_schedule._restart_gate_held(gateway) is False
    gateway._restart_gate_hold = loop.create_future()
    assert update_schedule._restart_gate_held(gateway) is True
    gateway._restart_gate_hold.set_result(None)
    assert update_schedule._restart_gate_held(gateway) is False


def test_an_unwired_or_mocked_probe_never_refuses():
    """Only a wired probe answering ``True`` refuses an update."""
    assert updates_apply._restart_pending_refusal(SimpleNamespace(_restart_gate_held=None)) is None
    assert updates_apply._restart_pending_refusal(SimpleNamespace()) is None
