"""The audit an inbound bridge action writes (RFC notification-bridge-actions, B4b).

Pins, through the real crew-log writer, that every action (admitted or refused)
writes one ``approval/bridged`` entry beside the decision, that an admitted one
writes no ``approval/decided`` of its own, that a token naming nothing is audited in
the SEL alone, and that a failing audit write never reaches the caller.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from crew_log_drain import assert_drained
from crew_log_emit_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    SESSION,
    _body,
    _isolated_home,
    _open_session,
)

from kiro_crew.notifications.bridge_action_audit import SEL_OPERATION, audit_action
from kiro_crew.notifications.bridge_actions import ActionOutcome, BridgeActionRegistry

_CHOICES = {"approve": True, "deny": False}
_DEADLINE = {"ttl_s": 300.0}  # flake-ok: a bind deadline nothing waits on


class _Sel:
    def __init__(self) -> None:
        self.accesses: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.accesses.append(kw)


@pytest.fixture()
def sel_log(monkeypatch) -> _Sel:
    log = _Sel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: log)
    return log


def _bridged() -> list[dict[str, Any]]:
    assert_drained()
    return [entry for entry in _body() if entry["type"].startswith("approval/")]


async def _bound(registry: BridgeActionRegistry) -> tuple[asyncio.Future, str]:
    decision = asyncio.get_running_loop().create_future()
    token = registry.bind(
        "dashboard:s1",
        "r1",
        decision,
        choices=_CHOICES,
        crew_log_session=SESSION,
        turn=3,
        owner="U-OWNER",
        **_DEADLINE,
    )
    return decision, token


async def _bound_other(registry: BridgeActionRegistry) -> str:
    return registry.bind(
        "dashboard:s1",
        "r2",
        asyncio.get_running_loop().create_future(),
        choices=_CHOICES,
        **_DEADLINE,
    )


@pytest.mark.asyncio
async def test_an_admitted_action_is_recorded_beside_the_decision(sel_log):
    _open_session()
    registry = BridgeActionRegistry()
    decision, token = await _bound(registry)
    outcome = registry.resolve(token, "approve", from_owner=True)
    audit_action(
        registry,
        token,
        outcome,
        transport="slack",
        choice="approve",
        actor="U-OWNER",
        channel="system.cron",
    )

    [entry] = _bridged()
    assert entry["type"] == "approval/bridged"
    assert entry["data"] == {
        "turn": 3,
        "approval_id": "r1",
        "transport": "slack",
        "outcome": "approved",
        "channel": "system.cron",
        "actor": "U-OWNER",
        "owner": "U-OWNER",
    }
    # The decision itself is the waiter's to record, as for a dashboard click.
    assert decision.result() is True
    [access] = sel_log.accesses
    assert access["caller"] == "dashboard:s1"
    assert access["operation"] == f"{SEL_OPERATION}:slack"
    assert access["outcome"] == "admitted"
    assert json.loads(access["resources"])["approval_id"] == "r1"


@pytest.mark.asyncio
async def test_an_admitted_deny_is_recorded_as_denied(sel_log):
    _open_session()
    registry = BridgeActionRegistry()
    decision, token = await _bound(registry)
    outcome = registry.resolve(token, "deny", from_owner=True)
    audit_action(registry, token, outcome, transport="slack", choice="deny", actor="U-OWNER")
    [entry] = _bridged()
    assert entry["data"]["outcome"] == "denied"
    assert "reason" not in entry["data"]
    assert decision.result() is False


@pytest.mark.asyncio
async def test_an_admitted_action_records_the_answer_the_registry_admitted(sel_log):
    """The caller's word cannot turn an admitted approve into a recorded deny."""
    _open_session()
    registry = BridgeActionRegistry()
    _decision, token = await _bound(registry)
    outcome = registry.resolve(token, "approve", from_owner=True)
    audit_action(registry, token, outcome, transport="slack", choice="deny")
    [entry] = _bridged()
    assert entry["data"]["outcome"] == "approved"
    assert json.loads(sel_log.accesses[0]["resources"])["choice"] == "approve"


@pytest.mark.asyncio
async def test_a_refusal_names_the_bound_owner_beside_the_stranger(sel_log):
    _open_session()
    registry = BridgeActionRegistry()
    _decision, token = await _bound(registry)
    outcome = registry.resolve(token, "approve", from_owner=False)
    audit_action(registry, token, outcome, transport="slack", choice="approve", actor="U-X")
    [entry] = _bridged()
    assert (entry["data"]["actor"], entry["data"]["owner"]) == ("U-X", "U-OWNER")
    resources = json.loads(sel_log.accesses[0]["resources"])
    assert (resources["actor"], resources["owner"]) == ("U-X", "U-OWNER")


@pytest.mark.asyncio
async def test_a_press_after_the_binding_was_pruned_is_audited_as_expired(sel_log, monkeypatch):
    """A late press is recorded as expired against its own session, not as a forgery."""
    clock = {"now": 1000.0}
    monkeypatch.setattr("kiro_crew.notifications.bridge_actions.bridge_clock", lambda: clock["now"])
    _open_session()
    registry = BridgeActionRegistry()
    _decision, token = await _bound(registry)
    clock["now"] += _DEADLINE["ttl_s"] + 1
    await _bound_other(registry)  # the next bind prunes the expired one
    outcome = registry.resolve(token, "approve", from_owner=True)
    assert outcome is ActionOutcome.EXPIRED
    audit_action(registry, token, outcome, transport="slack", choice="approve", actor="U-OWNER")
    [entry] = _bridged()
    assert (entry["data"]["outcome"], entry["data"]["approval_id"]) == ("expired", "r1")
    assert sel_log.accesses[0]["caller"] == "dashboard:s1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("act", "reason"),
    [
        (lambda reg, token: reg.resolve(token, "approve", from_owner=False), "not_owner"),
        (lambda reg, token: reg.resolve(token, "override", from_owner=True), "invalid_choice"),
        (lambda reg, token: reg.resolve(token, "trust", from_owner=True), "invalid_choice"),
    ],
)
async def test_a_refused_action_is_audited_too(sel_log, act, reason):
    _open_session()
    registry = BridgeActionRegistry()
    _decision, token = await _bound(registry)
    audit_action(registry, token, act(registry, token), transport="discord", actor="u2")
    [entry] = _bridged()
    assert entry["data"]["outcome"] == "refused"
    assert entry["data"]["reason"] == reason
    # The one who acted is on the record, the refused stranger included.
    assert entry["data"]["actor"] == "u2"
    assert [a["outcome"] for a in sel_log.accesses] == [reason]


@pytest.mark.asyncio
async def test_an_expired_action_is_recorded_as_expired(sel_log):
    _open_session()
    registry = BridgeActionRegistry()
    decision, token = await _bound(registry)
    decision.cancel()  # what asyncio.wait_for does to the future on timeout
    audit_action(
        registry,
        token,
        registry.resolve(token, "approve", from_owner=True),
        transport="slack",
        choice="approve",
    )
    [entry] = _bridged()
    assert entry["data"]["outcome"] == "expired"
    assert entry["data"]["reason"] == "expired"


@pytest.mark.asyncio
async def test_a_second_press_is_audited_as_already_answered(sel_log):
    _open_session()
    registry = BridgeActionRegistry()
    _decision, token = await _bound(registry)
    for _ in range(2):
        audit_action(
            registry,
            token,
            registry.resolve(token, "approve", from_owner=True),
            transport="slack",
            choice="approve",
        )
    assert [(e["data"]["outcome"], e["data"].get("reason")) for e in _bridged()] == [
        ("approved", None),
        ("refused", "already_answered"),
    ]


@pytest.mark.asyncio
async def test_a_token_that_names_nothing_is_audited_in_the_sel_alone(sel_log):
    _open_session()
    registry = BridgeActionRegistry()
    forged = "dashboard:s1:r9|not-a-nonce"
    audit_action(
        registry, forged, registry.resolve(forged, "approve", from_owner=True), transport="slack"
    )
    assert _bridged() == []
    [access] = sel_log.accesses
    assert access["caller"] == "_host"
    assert access["outcome"] == "unknown"
    assert "approval_id" not in json.loads(access["resources"])


@pytest.mark.asyncio
async def test_a_failing_audit_never_reaches_the_caller(monkeypatch):
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: type("S", (), {"log_api_access": _boom})())
    monkeypatch.setattr("kiro_crew.crew_log.emit.on_approval_bridged", _boom)
    registry = BridgeActionRegistry()
    _decision, token = await _bound(registry)
    audit_action(registry, token, ActionOutcome.ADMITTED, transport="slack")
