"""A conductor's patrol wake may drop its own chat -- and only that wake.

``reset_conversation`` is a user-surface directive: a turn a person started may
call it. The one non-human producer it admits is a monitor wake that the gateway
can vouch for (``session_directive_apply._refuse_unvouched_wake_reset``): the
wake's loop is this slot's current, active loop, the keystone-gated self-arm
record says the slot armed it itself, and nothing in the slot waits on a person.

The authorizer half: an ordinary dashboard slot arming itself through the
directive consumer now writes that self-arm record too, so a conductor that is
not in crew/member mode can be vouched for. An outside arm never writes it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import autonudge, autonudge_authz, autonudge_selfarm
from kiro_crew.autonudge_authz import authorize_and_add_nudge
from kiro_crew.dashboard import session_directive_apply as sda

SESSION = "dashboard:chat-1"
BINDING = "chat-1"
LOOP_ID = "a1b2c3d4"


class _Svc:
    """The loop store as the wake gate reads it: one current loop per slot."""

    def __init__(self, current: Any) -> None:
        self.current = current

    def get_by_slot(self, binding: str) -> Any:
        return self.current if binding == BINDING else None

    def get_by_id(self, loop_id: str) -> Any:
        return None


def _loop(loop_id: str = LOOP_ID, *, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=loop_id, slot_key=BINDING, active=active)


def _slot(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "key": BINDING,
        "agent": "kirocrew-conductor",
        "_app": "",
        "messages": [],
        "_question_pending": {},
        "_approval_futures": {},
        "_pending_discard_conversation_key": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _state(coordinator: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(pending_coordinator_approvals=lambda _key: list(coordinator or []))


@pytest.fixture
def wake_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A live, self-recorded loop on the slot; each test breaks one fact."""
    env: dict[str, Any] = {"svc": _Svc(_loop()), "recorded": {(LOOP_ID, BINDING)}, "audits": []}
    monkeypatch.setattr(autonudge, "get_instance", lambda: env["svc"])
    monkeypatch.setattr(
        autonudge_selfarm,
        "is_recorded_self_arm",
        lambda loop_id, slot_key: (loop_id, slot_key) in env["recorded"],
    )
    monkeypatch.setattr(sda, "_binding", lambda key: BINDING if key == SESSION else None)
    monkeypatch.setattr(sda, "_has_user_surface", lambda key: key == SESSION)
    monkeypatch.setattr(sda, "_audit", lambda key, kind, outcome: env["audits"].append(outcome))
    return env


async def _wake_reset(slot: Any, state: Any, *, loop_id: str = LOOP_ID, **flags: Any) -> str:
    flags.setdefault("producer_is_self_wake", True)
    return await sda.apply_session_directive(
        state,
        slot,
        SESSION,
        "reset_conversation",
        {},
        producer_wake_loop_id=loop_id,
        **flags,
    )


# ── admitted ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vouched_wake_queues_the_reset_and_logs_the_loop(
    wake_env: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    slot = _slot()
    with caplog.at_level(logging.WARNING, logger=sda.logger.name):
        result = await _wake_reset(slot, _state())
    assert result.startswith("Conversation reset queued")
    assert slot._pending_discard_conversation_key == SESSION
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(LOOP_ID in r.getMessage() and SESSION in r.getMessage() for r in warnings)
    assert "denied" not in wake_env["audits"]


# ── refused: the loop cannot be vouched for ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "loop_id"),
    [
        ("no loop id on the wake", ""),
        ("no loop on the slot", LOOP_ID),
        ("a different loop is current", LOOP_ID),
        ("the loop is stopped", LOOP_ID),
        ("the slot did not arm it itself", LOOP_ID),
    ],
)
async def test_unvouched_wake_is_refused(wake_env: dict[str, Any], case: str, loop_id: str) -> None:
    if case == "no loop on the slot":
        wake_env["svc"] = _Svc(None)
    elif case == "a different loop is current":
        wake_env["svc"] = _Svc(_loop("ffffffff"))
        wake_env["recorded"].add(("ffffffff", BINDING))
    elif case == "the loop is stopped":
        wake_env["svc"] = _Svc(_loop(active=False))
    elif case == "the slot did not arm it itself":
        # A cron, a person's dashboard arm or the bind-time default patrol: a
        # live loop with no self-arm record.
        wake_env["recorded"].clear()
    slot = _slot()
    result = await _wake_reset(slot, _state(), loop_id=loop_id)
    assert result.startswith("Conversation NOT reset"), case
    assert slot._pending_discard_conversation_key is None, case
    assert wake_env["audits"][-1] == "denied", case


@pytest.mark.asyncio
async def test_the_store_being_down_refuses(wake_env: dict[str, Any]) -> None:
    wake_env["svc"] = None
    slot = _slot()
    result = await _wake_reset(slot, _state())
    assert result.startswith("Conversation NOT reset")
    assert slot._pending_discard_conversation_key is None


# ── refused: something waits on a person ────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_question_card_refuses(wake_env: dict[str, Any]) -> None:
    slot = _slot(_question_pending={"card-1": {"blocking": False}})
    result = await _wake_reset(slot, _state())
    assert "question card or a tool approval" in result
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_pending_slot_approval_refuses(wake_env: dict[str, Any]) -> None:
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    slot = _slot(_approval_futures={"ap-1": future})
    result = await _wake_reset(slot, _state())
    assert "question card or a tool approval" in result
    assert slot._pending_discard_conversation_key is None
    future.cancel()


@pytest.mark.asyncio
async def test_settled_slot_approval_does_not_refuse(wake_env: dict[str, Any]) -> None:
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    future.set_result(True)
    slot = _slot(_approval_futures={"ap-1": future})
    result = await _wake_reset(slot, _state())
    assert result.startswith("Conversation reset queued")


@pytest.mark.asyncio
async def test_pending_coordinator_approval_refuses(wake_env: dict[str, Any]) -> None:
    slot = _slot()
    result = await _wake_reset(slot, _state([{"slot": BINDING}]))
    assert "question card or a tool approval" in result
    assert slot._pending_discard_conversation_key is None


# ── refused: not a wake at all ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_with_neither_mark_is_still_refused(wake_env: dict[str, Any]) -> None:
    """A cron, app or sub-agent turn carries neither the human nor the wake
    mark, so it meets the user-surface refusal exactly as before."""
    slot = _slot()
    result = await _wake_reset(slot, _state(), producer_is_self_wake=False)
    assert result.startswith("Error: reset_conversation only works from a user-facing session")
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_a_wake_still_may_not_set_the_project(wake_env: dict[str, Any]) -> None:
    result = await sda.apply_session_directive(
        _state(),
        _slot(),
        SESSION,
        "set_project",
        {"path": "/tmp/elsewhere"},
        producer_is_self_wake=True,
        producer_wake_loop_id=LOOP_ID,
    )
    assert result.startswith("Error: set_project only works from a user-facing session")


@pytest.mark.asyncio
async def test_a_human_turn_keeps_its_old_path(wake_env: dict[str, Any]) -> None:
    """A person's turn never meets the wake checks: no loop needed, and a
    pending card is the person's own business."""
    wake_env["svc"] = _Svc(None)
    slot = _slot(_question_pending={"card-1": {"blocking": False}})
    result = await sda.apply_session_directive(
        _state(), slot, SESSION, "reset_conversation", {}, producer_is_user_facing=True
    )
    assert result.startswith("Conversation reset queued")


# ── authorizer: who gets the self-arm record ────────────────────────────────


class _RecordingSvc:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def get_by_slot(self, slot_key: str) -> Any:
        return None

    def get_by_id(self, loop_id: str) -> Any:
        return None

    async def add(self, **kw: Any) -> Any:
        self.added.append(kw)
        return SimpleNamespace(
            id=kw.get("loop_id") or "loop-1",
            slot_key=kw["slot_key"],
            idle_secs=kw["idle_secs"],
            max_cycles=kw["max_cycles"],
            monitor=None,
            gate=kw.get("gate", False),
        )


@pytest.fixture
def arm_env(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    monkeypatch.setattr(
        autonudge_authz, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None)
    )
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        autonudge_authz, "record_self_arm", lambda loop_id, slot: writes.append((loop_id, slot))
    )
    return writes


async def _arm(svc: Any, tmp_path: Path, initiator: str, agent: str = "kirocrew-conductor") -> Any:
    slot = SimpleNamespace(workspace="default", mode="", memory_mode="persistent", agent=agent)
    return await authorize_and_add_nudge(
        svc=svc,
        state=SimpleNamespace(_slots={BINDING: slot}, sessions=None, channel_transports={}),
        slot_key=BINDING,
        message="patrol the ledger",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="mcp-directive",
        caller="session-directive",
        initiator_slot_key=initiator,
    )


@pytest.mark.asyncio
async def test_conductor_slot_arming_itself_is_recorded(
    arm_env: list[tuple[str, str]], tmp_path: Path
) -> None:
    svc = _RecordingSvc()
    loop, error, status = await _arm(svc, tmp_path, BINDING)
    assert error is None and status == 200 and loop is not None
    assert arm_env == [(loop.id, BINDING)]
    assert svc.added[0]["loop_id"] == loop.id
    # The crew/member fire-time bit is untouched on an ordinary slot.
    assert "self_armed" not in svc.added[0]


@pytest.mark.asyncio
async def test_outside_arm_of_a_conductor_slot_is_not_recorded(
    arm_env: list[tuple[str, str]], tmp_path: Path
) -> None:
    svc = _RecordingSvc()
    loop, error, status = await _arm(svc, tmp_path, "")
    assert status == 200 and loop is not None
    assert arm_env == []
    assert "loop_id" not in svc.added[0]


@pytest.mark.asyncio
async def test_ordinary_slot_still_arms_when_the_record_is_down(
    monkeypatch: pytest.MonkeyPatch, arm_env: list[tuple[str, str]], tmp_path: Path
) -> None:
    """The record only buys a wake reset; losing it must not cost the loop."""

    def _down(loop_id: str, slot: str) -> None:
        raise OSError("trust root unavailable")

    forgotten: list[str] = []
    monkeypatch.setattr(autonudge_authz, "record_self_arm", _down)
    monkeypatch.setattr(autonudge_authz, "forget_self_arm", forgotten.append)
    svc = _RecordingSvc()
    loop, error, status = await _arm(svc, tmp_path, BINDING)
    assert error is None and status == 200 and loop is not None
    assert len(svc.added) == 1
    assert forgotten == []


@pytest.mark.asyncio
async def test_ordinary_slot_record_is_forgotten_when_the_arm_fails(
    monkeypatch: pytest.MonkeyPatch, arm_env: list[tuple[str, str]], tmp_path: Path
) -> None:
    """A record written for an arm that never committed must not outlive it."""
    from kiro_crew.autonudge_service.model import NudgeAdmissionRefused

    forgotten: list[str] = []
    monkeypatch.setattr(autonudge_authz, "forget_self_arm", forgotten.append)

    class _RefusingSvc(_RecordingSvc):
        async def add(self, **kw: Any) -> Any:
            raise NudgeAdmissionRefused("session changed")

    loop, error, status = await _arm(_RefusingSvc(), tmp_path, BINDING)
    assert loop is None and status == 409
    assert [loop_id for loop_id, _slot in arm_env] == forgotten
    assert len(forgotten) == 1


@pytest.mark.asyncio
async def test_a_pause_during_the_trust_read_refuses(wake_env: dict[str, Any]) -> None:
    """The loop is re-read after the off-loop trust read: a Pause that lands
    inside that await must still win."""
    calls: list[str] = []

    def _record_then_pause(loop_id: str, slot_key: str) -> bool:
        calls.append(loop_id)
        wake_env["svc"].current.active = False
        return True

    import kiro_crew.autonudge_selfarm as selfarm

    selfarm_patch = pytest.MonkeyPatch()
    selfarm_patch.setattr(selfarm, "is_recorded_self_arm", _record_then_pause)
    try:
        slot = _slot()
        result = await _wake_reset(slot, _state())
    finally:
        selfarm_patch.undo()
    assert calls == [LOOP_ID]
    assert result.startswith("Conversation NOT reset")
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_only_a_wake_reset_is_marked_to_wait_at_the_boundary(
    wake_env: dict[str, Any],
) -> None:
    wake_slot = _slot(_pending_discard_from_wake=False)
    await _wake_reset(wake_slot, _state())
    assert wake_slot._pending_discard_from_wake is True

    person_slot = _slot(_pending_discard_from_wake=True)
    await sda.apply_session_directive(
        _state(), person_slot, SESSION, "reset_conversation", {}, producer_is_user_facing=True
    )
    assert person_slot._pending_discard_from_wake is False


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["", "kirocrew", "kirocrew-worker"])
async def test_a_non_conductor_wake_is_refused(wake_env: dict[str, Any], agent: str) -> None:
    """A self-armed loop on any other agent's slot passes every loop check, so
    the agent check is what keeps the exception to the conductor."""
    slot = _slot(agent=agent)
    result = await _wake_reset(slot, _state())
    assert result.startswith("Conversation NOT reset"), agent
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_the_deprecated_conductor_alias_is_admitted(wake_env: dict[str, Any]) -> None:
    slot = _slot(agent="kirocrew-ledger-conductor")
    result = await _wake_reset(slot, _state())
    assert result.startswith("Conversation reset queued")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["", "kirocrew", "kirocrew-worker"])
async def test_other_agents_arming_themselves_are_not_recorded(
    arm_env: list[tuple[str, str]], tmp_path: Path, agent: str
) -> None:
    """The record's only ordinary-slot reader is the conductor wake-reset gate,
    so no other agent's self-arm writes it."""
    svc = _RecordingSvc()
    loop, error, status = await _arm(svc, tmp_path, BINDING, agent=agent)
    assert status == 200 and loop is not None
    assert arm_env == []
    assert "loop_id" not in svc.added[0]
