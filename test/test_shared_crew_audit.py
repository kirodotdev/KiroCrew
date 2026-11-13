"""Shared Crew action audit: what a record carries and what a failed write means."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from kiro_crew.shared_crew import audit
from kiro_crew.shared_crew.contracts import (
    ActionOutcome,
    ActionType,
    ActorContext,
    Provenance,
    Transport,
)
from kiro_crew.shared_crew.identity import tailnet_principal
from kiro_crew.testing.wait import await_until

ALICE = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_SERVE)
BOB = tailnet_principal("bob@example.com", "b", Provenance.TAILSCALE_DIRECT)


def _actor(p, correlation: str = "corr-1") -> ActorContext:
    return ActorContext(
        principal=p, request_id="req-1", transport=Transport.HTTP, correlation_id=correlation
    )


def _record(**kw: Any):
    return audit.new_action_record(
        ActionType.APPROVAL,
        session_key="dashboard:chat-1",
        initiator=_actor(ALICE),
        responder=_actor(BOB),
        outcome=ActionOutcome.APPLIED,
        target="approval-7",
        **kw,
    )


@pytest.mark.asyncio
async def test_a_recorded_action_names_actor_target_outcome_and_correlation():
    written: list[Any] = []
    record = _record()
    assert await audit.record_action(record, writer=written.append) is True
    (event,) = written
    assert event.event_type == "shared_crew_action"
    assert event.operation == "shared_crew.approval"
    assert event.outcome == "applied"
    assert event.caller_identity == ALICE.id
    assert event.resources == "approval-7"
    assert event.request_id == "corr-1"
    meta = event.metadata
    assert meta["initiator"] == {
        "id": ALICE.id,
        "kind": "human",
        "provenance": "tailscale_serve",
        "login": "alice@example.com",
    }
    assert meta["responder"]["id"] == BOB.id
    assert meta["session_key"] == "dashboard:chat-1"


@pytest.mark.asyncio
async def test_a_record_carries_no_prompt_text_tokens_or_subjects():
    written: list[Any] = []
    await audit.record_action(_record(), writer=written.append)
    blob = json.dumps(written[0].metadata) + written[0].resources
    for forbidden in ("token", "cookie", "content", "prompt", "issuer", "subject"):
        assert forbidden not in blob


@pytest.mark.asyncio
async def test_a_refused_write_is_reported_as_not_recorded_and_never_raises():
    def refuse(_event: Any) -> None:
        raise OSError("disk full")

    assert await audit.record_action(_record(), writer=refuse) is False


@pytest.mark.asyncio
async def test_a_write_that_does_not_confirm_in_time_is_not_claimed():
    release = threading.Event()

    def stuck(_event: Any) -> None:
        release.wait(timeout=5)

    try:
        assert await audit.record_action(_record(), writer=stuck, timeout=0.05) is False
    finally:
        release.set()


@pytest.mark.asyncio
async def test_long_ids_are_bounded():
    record = audit.new_action_record(
        ActionType.MESSAGE,
        session_key="k" * 5000,
        initiator=_actor(ALICE, correlation="c"),
        outcome=ActionOutcome.ACCEPTED,
        target="t" * 5000,
    )
    assert len(record.session_key) == 256 and len(record.target) == 256


@pytest.mark.asyncio
async def test_scheduled_audit_runs_without_blocking_its_caller(monkeypatch):
    written: list[Any] = []
    monkeypatch.setattr(audit, "_default_writer", written.append)
    rec = audit.schedule_action_audit(
        ActionType.MESSAGE,
        session_key="dashboard:chat-1",
        initiator=_actor(ALICE),
        outcome=ActionOutcome.ACCEPTED,
        target="m-1",
    )
    assert rec is not None and rec.action_type is ActionType.MESSAGE
    assert not written, "the caller returns before the write"
    (task,) = list(audit._pending_audits)
    assert await await_until(task) is True
    assert written[0].metadata["action_id"] == rec.action_id


def test_scheduling_without_a_loop_records_nothing_and_does_not_raise():
    assert (
        audit.schedule_action_audit(
            ActionType.MESSAGE,
            session_key="s",
            initiator=_actor(ALICE),
            outcome=ActionOutcome.ACCEPTED,
        )
        is None
    )
