"""The single ``agent.kas_workflows`` opt-in gate.

One gateway setting replaced the per-device preview flag and the separate
chat-workflows toggle. Every KAS surface now asks one question --
:func:`kiro_crew.kas_workflows.engine.kas_workflows_enabled` -- which is true
only when the backend is Kiro Agent AND the operator opted in. These tests pin
that gate across the (backend x switch) matrix at each place it is read: the
engine endpoint, the route refusal, boot recovery and the chat surface.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.kas_workflows.engine import (
    ENGINE_CREW,
    ENGINE_KAS,
    backend_brings_kas_workflows,
    engine_for,
    kas_workflows_enabled,
)

# `kas` is a native-workflows backend; `""` is not. (engine_for pins this.)
KAS = "kas"
NON_KAS = ""


def _cfg(backend: str, switch: bool):
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, kas_workflows=switch))


# ── The gate itself: both halves must hold ──


@pytest.mark.parametrize(
    ("backend", "switch", "expected"),
    [(KAS, True, True), (KAS, False, False), (NON_KAS, True, False), (NON_KAS, False, False)],
)
def test_gate_requires_kas_backend_and_the_switch(backend, switch, expected):
    assert engine_for(KAS) == ENGINE_KAS  # fixture sanity
    assert kas_workflows_enabled(_cfg(backend, switch)) is expected


def test_backend_capability_is_independent_of_the_switch():
    assert backend_brings_kas_workflows(KAS) is True
    assert backend_brings_kas_workflows(NON_KAS) is False


def test_a_configless_object_is_off():
    assert kas_workflows_enabled(SimpleNamespace()) is False


# ── /api/workflows/engine answers for each (backend x switch) pair ──


async def _engine_answer(backend, switch, monkeypatch):
    from kiro_crew.dashboard.handlers import workflows as wf

    monkeypatch.setattr(
        "kiro_crew.config.KiroCrewConfig.load", staticmethod(lambda: _cfg(backend, switch))
    )
    resp = await wf.api_workflow_engine(make_mocked_request("GET", "/api/workflows/engine"))
    return json.loads(resp.text)


@pytest.mark.parametrize(
    ("backend", "switch", "engine"),
    [
        (KAS, True, ENGINE_KAS),
        (KAS, False, ENGINE_CREW),
        (NON_KAS, True, ENGINE_CREW),
        (NON_KAS, False, ENGINE_CREW),
    ],
)
@pytest.mark.asyncio
async def test_engine_endpoint_reports_kas_only_when_enabled(backend, switch, engine, monkeypatch):
    body = await _engine_answer(backend, switch, monkeypatch)
    assert body["engine"] == engine
    # Raw backend and the two explainer fields ride along for the settings UI.
    assert body["backend"] == backend
    assert body["kas_backend"] is (backend == KAS)
    assert body["kas_workflows"] is switch


# ── The KAS routes refuse unless the gate is open ──


async def _refusal_response(backend, switch, monkeypatch):
    from kiro_crew.dashboard.handlers import kas_workflows as handlers

    monkeypatch.setattr(
        "kiro_crew.config.KiroCrewConfig.load", staticmethod(lambda: _cfg(backend, switch))
    )

    async def _owner_ok(_request, _operation):
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request", _owner_ok
    )
    return await handlers._refusal(make_mocked_request("GET", "/api/workflows/kas/state"), "op")


@pytest.mark.asyncio
async def test_routes_answer_only_when_enabled(monkeypatch):
    # Open gate -> no refusal.
    assert await _refusal_response(KAS, True, monkeypatch) is None


@pytest.mark.parametrize(("backend", "switch"), [(KAS, False), (NON_KAS, True), (NON_KAS, False)])
@pytest.mark.asyncio
async def test_routes_refuse_engine_not_kas_when_off(backend, switch, monkeypatch):
    resp = await _refusal_response(backend, switch, monkeypatch)
    assert resp is not None and resp.status == 409
    assert b"engine_not_kas" in resp.body


# ── Boot recovery and the chat surface read the same gate ──


def test_boot_recovery_follows_the_gate():
    from kiro_crew.dashboard.handlers.kas_workflows import boot_recovery_enabled

    assert boot_recovery_enabled(_cfg(KAS, True)) is True
    assert boot_recovery_enabled(_cfg(KAS, False)) is False
    assert boot_recovery_enabled(_cfg(NON_KAS, True)) is False


def test_chat_surface_follows_the_gate(monkeypatch):
    from kiro_crew.kas_workflows import chat

    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: _cfg(KAS, True))
    assert chat.chat_workflows_enabled() is True
    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: _cfg(KAS, False))
    assert chat.chat_workflows_enabled() is False
    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: _cfg(NON_KAS, True))
    assert chat.chat_workflows_enabled() is False


# ── Gap 5: switch off but the router still holds a run — reads + Stop stay open ──


class _FakeRouter:
    def __init__(self, held: int) -> None:
        self._rows = [{"workflowId": f"wf_{i:08x}"} for i in range(held)]

    def rows(self) -> list[dict]:
        return self._rows


_READS = [
    "workflow.kas.state",
    "workflow.kas.run",
    "workflow.kas.slot_runs",
    "workflow.kas.step_transcript",
    "workflow.kas.runs",
]
# Launch and control (other than Stop) and every answer stay refused.
_REFUSED_CONTROLS = [
    "workflow.kas.start",
    "workflow.kas.grant",
    "workflow.kas.retry",
    "workflow.kas.restart",
    "workflow.kas.approve",
    "workflow.kas.question",
    "workflow.kas.step_reply",
    "workflow.kas.recipes",
    "workflow.kas.reconnect",
]


async def _refusal_held(operation, *, backend, held, monkeypatch, action=None):
    from kiro_crew.dashboard.handlers import kas_workflows as handlers

    monkeypatch.setattr(
        "kiro_crew.config.KiroCrewConfig.load", staticmethod(lambda: _cfg(backend, False))
    )

    async def _owner_ok(_request, _operation):
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request", _owner_ok
    )
    monkeypatch.setattr("kiro_crew.kas_workflows.router.get_router", lambda: _FakeRouter(held))
    req = make_mocked_request("POST", "/api/workflows/kas/x")
    if action is not None:
        req._match_info = {"action": action}  # the control route's path param
    return await handlers._refusal(req, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", _READS)
async def test_off_but_held_allows_reads(operation, monkeypatch):
    assert await _refusal_held(operation, backend=KAS, held=1, monkeypatch=monkeypatch) is None


@pytest.mark.asyncio
async def test_off_but_held_allows_only_stop_among_controls(monkeypatch):
    assert (
        await _refusal_held(
            "workflow.kas.control", backend=KAS, held=1, monkeypatch=monkeypatch, action="cancel"
        )
        is None
    )
    for action in ("resume", "pause", "delete"):
        resp = await _refusal_held(
            "workflow.kas.control", backend=KAS, held=1, monkeypatch=monkeypatch, action=action
        )
        assert resp is not None and resp.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", _REFUSED_CONTROLS)
async def test_off_but_held_refuses_launch_and_control(operation, monkeypatch):
    resp = await _refusal_held(operation, backend=KAS, held=1, monkeypatch=monkeypatch)
    assert resp is not None and resp.status == 409 and b"engine_not_kas" in resp.body


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [*_READS, "workflow.kas.control"])
async def test_off_with_no_held_runs_refuses_everything(operation, monkeypatch):
    # Origin/main: with nothing held, even the reads refuse.
    resp = await _refusal_held(
        operation, backend=KAS, held=0, monkeypatch=monkeypatch, action="cancel"
    )
    assert resp is not None and resp.status == 409


@pytest.mark.asyncio
async def test_off_on_a_non_kas_backend_has_no_read_window(monkeypatch):
    # Not a Kiro Agent backend: no read-only window even if a stray row is held.
    resp = await _refusal_held(
        "workflow.kas.state", backend=NON_KAS, held=1, monkeypatch=monkeypatch
    )
    assert resp is not None and resp.status == 409


# ── Gap 5b: attach_workflow_settings sends an EXPLICIT enabled ──


def test_attach_workflow_settings_sends_explicit_enabled():
    from kiro_crew.kas_workflows.chat import attach_workflow_settings

    on: dict = {}
    attach_workflow_settings(on, creator_effort="high")
    assert on["_meta"]["kiro"]["settings"]["workflows"] == {
        "enabled": True,
        "creatorEffort": "high",
    }

    off: dict = {}
    attach_workflow_settings(off, enabled=False, creator_effort="high")
    workflows = off["_meta"]["kiro"]["settings"]["workflows"]
    # Explicit False (not absent), and no creatorEffort rides along when off.
    assert workflows["enabled"] is False and "creatorEffort" not in workflows
