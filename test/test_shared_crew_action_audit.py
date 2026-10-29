"""Work cancellation and shared decision responses reach the Shared Crew audit trail.

The security event log write is replaced by a collector at
``audit._default_writer``, so each test reads the events that would have been
written and nothing touches the operator's log.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.shared_crew import admission_gate, audit, routes_admission
from kiro_crew.shared_crew.admission import OperationKind, StaleRevision
from kiro_crew.shared_crew.collab import DecisionKind
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.decisions import DecisionRegistry


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


ALICE = _person("alice@example.test")
BOB = _person("bob@example.test")
CAROL = _person("carol@example.test")


@pytest.fixture
def events(monkeypatch) -> list[Any]:
    seen: list[Any] = []
    monkeypatch.setattr(audit, "_default_writer", seen.append)
    return seen


async def _drain() -> None:
    while audit._pending_audits:
        await asyncio.gather(*list(audit._pending_audits))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,action",
    [(DecisionKind.QUESTION, "question_answer"), (DecisionKind.APPROVAL, "approval")],
)
async def test_a_decision_response_is_audited_with_initiator_and_responder(events, kind, action):
    reg = DecisionRegistry()
    reg.open("d1", kind, "s1", ALICE)
    won = reg.resolve("d1", response_id="b", responder=BOB, outcome="yes", apply=lambda: True)
    replay = reg.resolve("d1", response_id="b", responder=BOB, outcome="yes", apply=lambda: True)
    lost = reg.resolve("d1", response_id="c", responder=CAROL, outcome="no", apply=lambda: True)
    reg.resolve("missing", response_id="x", responder=BOB, outcome="yes", apply=lambda: True)
    await _drain()

    assert won.applied and not replay.applied and not lost.applied
    assert [(e.operation, e.outcome) for e in events] == [
        (f"shared_crew.{action}", "applied"),
        (f"shared_crew.{action}", "conflict"),
    ]
    first, second = (e.metadata for e in events)
    assert first["initiator"]["id"] == ALICE.id and first["responder"]["id"] == BOB.id
    assert second["responder"]["id"] == CAROL.id
    assert first["target"] == "d1" and first["session_key"] == "s1"
    # The answer itself is never written.
    assert "yes" not in repr(events[0].metadata)


class _Service:
    def __init__(self, *, stale: bool = False) -> None:
        self.stale = stale
        self.work = SimpleNamespace(
            session_key="sess-1", to_public_dict=lambda: {"work_id": "w1", "state": "cancelling"}
        )

    async def cancel(self, work_id: str, *, expected_revision: int) -> Any:
        if self.stale:
            raise StaleRevision(expected_revision, expected_revision + 1)
        return self.work

    async def current_work(self, work_id: str) -> Any:
        return None


def _client(service: _Service, monkeypatch, app_claim: str | None = None) -> TestClient:
    async def current_service() -> _Service:
        return service

    monkeypatch.setattr(admission_gate, "current_service", current_service)

    @web.middleware
    async def mw(request, handler):
        if app_claim is not None:
            request["app"] = app_claim
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=BOB, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    app = web.Application(middlewares=[mw])
    routes_admission.register(app)
    return TestClient(TestServer(app))


@pytest.mark.asyncio
async def test_a_cancellation_is_audited(events, monkeypatch):
    async with _client(_Service(), monkeypatch) as client:
        resp = await client.post("/api/shared-crew/work/w1/cancel", json={"expected_revision": 3})
        assert resp.status == 200
    await _drain()
    assert [(e.operation, e.outcome, e.resources) for e in events] == [
        ("shared_crew.cancel", "applied", "w1")
    ]
    assert events[0].metadata["initiator"]["id"] == BOB.id
    assert events[0].metadata["session_key"] == "sess-1"


@pytest.mark.asyncio
async def test_a_stale_cancellation_is_audited_as_conflict(events, monkeypatch):
    async with _client(_Service(stale=True), monkeypatch) as client:
        resp = await client.post("/api/shared-crew/work/w1/cancel", json={"expected_revision": 3})
        assert resp.status == 409
    await _drain()
    assert [(e.operation, e.outcome) for e in events] == [("shared_crew.cancel", "conflict")]


@pytest.mark.asyncio
async def test_a_late_different_response_is_audited_as_conflict(events):
    reg = DecisionRegistry()
    reg.open("d2", DecisionKind.APPROVAL, "s1", ALICE)
    reg.resolve("d2", response_id="b", responder=BOB, outcome="yes", apply=lambda: True)
    # The waiting future is gone, so these arrive at the settled decision.
    reg.classify_late("d2", "b", BOB)
    reg.classify_late("d2", "c", CAROL)
    reg.classify_late("missing", "c", CAROL)
    await _drain()
    assert [(e.operation, e.outcome, e.metadata["responder"]["id"]) for e in events] == [
        ("shared_crew.approval", "applied", BOB.id),
        ("shared_crew.approval", "conflict", CAROL.id),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,owner,status",
    [
        (OperationKind.TASK, False, 403),
        (OperationKind.CRON, False, 403),
        (OperationKind.TASK, True, 200),
        (OperationKind.CHAT_TURN, False, 200),
    ],
)
async def test_only_the_owner_cancels_a_planned_task_or_schedule_run(
    events, monkeypatch, operation, owner, status
):
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: owner)
    service = _Service()
    cancelled: list[str] = []

    async def current_work(work_id: str) -> Any:
        return SimpleNamespace(operation=operation)

    async def cancel(work_id: str, *, expected_revision: int) -> Any:
        cancelled.append(work_id)
        return service.work

    service.current_work = current_work  # type: ignore[method-assign]
    service.cancel = cancel  # type: ignore[method-assign]
    async with _client(service, monkeypatch, app_claim="") as client:
        resp = await client.post("/api/shared-crew/work/w1/cancel", json={"expected_revision": 3})
        assert resp.status == status
    assert cancelled == ([] if status == 403 else ["w1"])
