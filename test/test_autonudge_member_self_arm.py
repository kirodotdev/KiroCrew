"""Crew/member slots may arm a loop on THEMSELVES; outsiders still may not.

The defect these pin: ``autonudge_authz`` refused every arm on a crew- or
member-mode slot with "<mode>-mode sessions do not accept direct automation
turns". The intent of that refusal is to keep a cron, another session or an app
from injecting automation turns into a member's own thread. The side effect
was that a member's OWN ``monitor_start`` was refused too, and the MCP tool
had already answered "requested" over its own pipe, so nothing told anyone:
the conductor member thread armed its patrol loop, ended its turn, and was
never woken again -- ``autonudge.json`` never held the loop.

Three states, each pinned here:

(a) SELF-ARM ADMITTED -- the arm request came from the target session's own
    turn (``initiator_slot_key == slot_key``): the loop is armed, the SEL trail
    carries a ``self_armed`` outcome, the record persists ``self_armed=True``,
    and the fire-time re-check lets it wake.
(b) EXTERNAL STILL REFUSED -- no initiator, or an initiator that is a
    different session: 409, nothing armed, unchanged from before.
(c) REFUSAL VISIBLE -- a refused ``monitor_start``/``monitor_watch`` directive
    writes a ``notice`` row into the session transcript instead of dying in
    the log.

Every test patches ``sel`` so nothing is written to the real security event log.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import autonudge_authz
from kiro_crew.autonudge import (
    AutoNudgeService,
    MonitorUpdateConflict,
    NudgeAdmissionRefused,
    NudgeLoop,
)
from kiro_crew.autonudge_authz import (
    authorize_and_add_nudge,
    authorize_and_update_monitor,
    external_arm_refusal,
    is_self_arm,
)
from kiro_crew.dashboard import session_directive_apply as sda
from kiro_crew.monitoring.models import MonitorBudgets, MonitorState

# ── fixtures ────────────────────────────────────────────────────────────────


class RecordingSvc:
    """Minimal AutoNudgeService stand-in that records what it was asked to arm."""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.added_monitors: list[dict[str, Any]] = []

    def get_by_slot(self, slot_key: str) -> Any:
        return None

    def get_by_id(self, loop_id: str) -> Any:
        """The authorizer reserves a self-arm id against this; nothing is taken."""
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

    async def add_monitor(self, **kw: Any) -> Any:
        self.added_monitors.append(kw)
        return SimpleNamespace(id=kw.get("loop_id") or "monitor-1", slot_key=kw["slot_key"])


def _state(slots: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(_slots=slots, sessions=None, channel_transports={})


def _member_slot(mode: str = "member") -> SimpleNamespace:
    return SimpleNamespace(workspace="default", mode=mode, memory_mode="persistent")


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    return events


@pytest.fixture
def trust_record(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture the keystone-gated self-arm record write instead of touching
    the real ``trust/`` root."""
    writes: list[tuple[str, str]] = []

    def _record(loop_id: str, slot_key: str) -> None:
        writes.append((loop_id, slot_key))

    monkeypatch.setattr(autonudge_authz, "record_self_arm", _record)
    return writes


def _monitor() -> MonitorState:
    return MonitorState(
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        created_ts=1_000.0,
        budgets=MonitorBudgets(
            max_runtime_secs=14_400,
            max_agent_turns=8,
            max_tokens=250_000,
            max_provider_errors=3,
        ),
        cadence_secs=300,
        wake_instructions="Inspect the blocker.",
    )


# ── is_self_arm: the predicate itself ───────────────────────────────────────


def test_is_self_arm_requires_a_non_blank_exact_match() -> None:
    assert is_self_arm("member-conductor", "member-conductor")
    assert is_self_arm(" member-conductor ", "member-conductor")
    # Blank never matches blank: a caller that failed to resolve the target key
    # must not self-arm by accident.
    assert not is_self_arm("", "")
    assert not is_self_arm("member-conductor", "")
    assert not is_self_arm("member-conductor", "chat-9-9")


def test_external_arm_refusal_keeps_the_mode_prefix_and_names_the_exception() -> None:
    reason = external_arm_refusal("member")
    assert reason.startswith("member-mode sessions do not accept direct automation turns")
    assert "own turn" in reason


# ── (a) self-arm admitted ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["crew", "member"])
async def test_add_admits_a_slot_arming_itself(
    audits: list[dict[str, Any]],
    trust_record: list[tuple[str, str]],
    tmp_path: Path,
    mode: str,
) -> None:
    svc = RecordingSvc()
    slot = _member_slot(mode)

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": slot}),
        slot_key="member-conductor",
        message="patrol the worker sessions",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="mcp-directive",
        caller="session-directive",
        initiator_slot_key="member-conductor",
    )

    assert error is None and status == 200 and loop is not None
    assert len(svc.added) == 1
    # The record is marked so the fire-time re-check can honour it ...
    assert svc.added[0]["self_armed"] is True
    # ... and the keystone-gated trust record, written BEFORE the add, names the
    # pre-minted id the service was handed, on this slot.
    assert len(trust_record) == 1
    recorded_id, recorded_slot = trust_record[0]
    assert recorded_slot == "member-conductor"
    assert svc.added[0]["loop_id"] == recorded_id == loop.id
    assert re.fullmatch(r"[0-9a-f]{8}", recorded_id)
    # SEL: a distinct outcome for the self-arm, then the ordinary trail.
    outcomes = [event["outcome"] for event in audits]
    assert "self_armed" in outcomes
    assert outcomes[-2:] == ["invoked", "success"]
    invoked = next(event for event in audits if event["outcome"] == "invoked")
    assert invoked["metadata"]["self_armed"] is True


@pytest.mark.asyncio
async def test_self_armed_admission_holds_its_mode_but_not_a_mode_switch(
    audits: list[dict[str, Any]], trust_record: list[tuple[str, str]], tmp_path: Path
) -> None:
    """TOCTOU guard: a self-armed member loop commits while the slot is still
    a member; it must NOT commit if the slot changed mode in between."""
    svc = RecordingSvc()
    slot = _member_slot("member")
    state = _state({"member-conductor": slot})

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key="member-conductor",
        message="patrol",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="mcp-directive",
        initiator_slot_key="member-conductor",
    )
    assert status == 200 and loop is not None
    admission_check = svc.added[0]["admission_check"]
    assert admission_check()
    slot.mode = "crew"
    assert not admission_check()
    slot.mode = "member"
    assert admission_check()
    slot.memory_mode = "temporary"
    assert not admission_check()


@pytest.mark.asyncio
async def test_structured_monitor_watch_admits_a_slot_arming_itself(
    audits: list[dict[str, Any]],
    trust_record: list[tuple[str, str]],
) -> None:
    svc = RecordingSvc()
    slot = _member_slot("member")

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": slot}),
        slot_key="member-conductor",
        message="structured monitor",
        source="mcp-directive",
        monitor=_monitor(),
        initiator_slot_key="member-conductor",
    )

    assert error is None and status == 200 and loop is not None
    assert svc.added_monitors[0]["self_armed"] is True
    assert "self_armed" in [event["outcome"] for event in audits]


@pytest.mark.asyncio
async def test_update_monitor_admits_a_member_patching_its_own_monitor(
    audits: list[dict[str, Any]],
) -> None:
    svc = SimpleNamespace(update_monitor=AsyncMock(return_value=SimpleNamespace(id="monitor-1")))
    slot = _member_slot("member")

    loop, error, status = await authorize_and_update_monitor(
        svc=svc,
        state=_state({"member-conductor": slot}),
        loop_id="monitor-1",
        session_key="member-conductor",
        patch={"cadence_secs": 300},
        source="mcp-directive",
        initiator_slot_key="member-conductor",
    )

    assert error is None and status == 200 and loop is not None
    svc.update_monitor.assert_awaited_once()
    assert [event["outcome"] for event in audits] == ["invoked", "self_armed"]


@pytest.mark.asyncio
async def test_update_monitor_denies_a_self_arm_whose_grant_audit_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``self_armed`` grant audit is audit-or-deny like ``invoked``: a SEL
    that accepts the ``invoked`` record but fails on the grant must NOT let the
    member's mutation run unaudited."""
    outcomes: list[str] = []

    def _log(**kw: Any) -> None:
        outcomes.append(kw["outcome"])
        if kw["outcome"] == "self_armed":
            raise OSError("sel unwritable")

    monkeypatch.setattr(autonudge_authz, "sel", lambda: SimpleNamespace(log_tool_invocation=_log))
    svc = SimpleNamespace(update_monitor=AsyncMock(return_value=SimpleNamespace(id="monitor-1")))
    slot = _member_slot("member")

    loop, error, status = await authorize_and_update_monitor(
        svc=svc,
        state=_state({"member-conductor": slot}),
        loop_id="monitor-1",
        session_key="member-conductor",
        patch={"cadence_secs": 300},
        source="mcp-directive",
        initiator_slot_key="member-conductor",
    )

    assert loop is None and status == 503
    assert error == "audit log unavailable — monitor not updated"
    svc.update_monitor.assert_not_awaited()
    assert outcomes == ["invoked", "self_armed"]


@pytest.mark.asyncio
async def test_self_armed_persists_through_the_store(tmp_path: Path) -> None:
    """A restart re-arms every loop; a self-armed member loop that lost the bit
    would be refused at its first post-restart wake, so it must round-trip."""
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        loop = await svc.add(
            slot_key="member-conductor",
            message="patrol",
            idle_secs=300,
            self_armed=True,
        )
        assert loop.self_armed is True
        reloaded = AutoNudgeService(base_dir=tmp_path)
        try:
            reloaded._load()
            stored = reloaded.get_by_slot("member-conductor")
            assert stored is not None and stored.self_armed is True
        finally:
            reloaded.stop()
        # A record written before the field existed decodes to False.
        assert NudgeLoop(id="x", slot_key="chat-1-1", message="m").self_armed is False
    finally:
        svc.stop()


# ── (b) external arms still refused ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["crew", "member"])
@pytest.mark.parametrize("initiator", ["", "chat-9-9", "member-other"])
async def test_add_still_refuses_an_arm_from_outside_the_session(
    audits: list[dict[str, Any]], mode: str, initiator: str
) -> None:
    svc = RecordingSvc()
    slot = _member_slot(mode)

    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": slot}),
        slot_key="member-conductor",
        message="inject work",
        source="dashboard",
        initiator_slot_key=initiator,
    )

    assert loop is None and status == 409
    assert error is not None and f"{mode}-mode" in error
    assert svc.added == []
    assert [event["outcome"] for event in audits] == ["denied"]


@pytest.mark.asyncio
async def test_structured_monitor_watch_still_refuses_an_outside_arm(
    audits: list[dict[str, Any]],
) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": _member_slot("member")}),
        slot_key="member-conductor",
        message="structured monitor",
        source="workflow",
        monitor=_monitor(),
    )
    assert loop is None and status == 409 and error is not None
    assert svc.added_monitors == []


@pytest.mark.asyncio
async def test_update_monitor_still_refuses_an_outside_patch(
    audits: list[dict[str, Any]],
) -> None:
    svc = SimpleNamespace(update_monitor=AsyncMock(return_value=SimpleNamespace(id="monitor-1")))
    loop, error, status = await authorize_and_update_monitor(
        svc=svc,
        state=_state({"member-conductor": _member_slot("crew")}),
        loop_id="monitor-1",
        session_key="member-conductor",
        patch={"cadence_secs": 300},
        source="dashboard",
    )
    assert loop is None and status == 409 and error is not None
    svc.update_monitor.assert_not_awaited()
    assert [event["outcome"] for event in audits] == ["invoked", "denied"]


@pytest.mark.asyncio
async def test_external_arm_kwargs_shape_is_unchanged(
    audits: list[dict[str, Any]], tmp_path: Path
) -> None:
    """A default-mode slot armed from REST never sees the new kwarg, so the
    ``svc.add`` contract other tests pin by equality is untouched."""
    svc = RecordingSvc()
    slot = SimpleNamespace(workspace="default", mode="", memory_mode="persistent")
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"chat-1-1": slot}),
        slot_key="chat-1-1",
        message="watch",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
    )
    assert status == 200 and loop is not None
    assert "self_armed" not in svc.added[0]
    assert "loop_id" not in svc.added[0]
    assert "self_armed" not in [event["outcome"] for event in audits]


# ── directive consumer: provenance + (c) visible refusal ────────────────────


@pytest.mark.asyncio
async def test_monitor_start_directive_passes_its_own_binding_as_initiator() -> None:
    """The consumer applies a directive to the exact session whose turn
    produced it, so it -- and only it -- may vouch for a self-arm."""
    captured: dict[str, Any] = {}

    async def _authz(**kw: Any) -> tuple[Any, None, int]:
        captured.update(kw)
        return SimpleNamespace(id="loop-1", monitor=None, gate=False), None, 200

    state = SimpleNamespace()
    slot = SimpleNamespace(key="member-conductor", _app="", messages=[])
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _authz),
        patch.object(sda, "_audit"),
    ):
        result = await sda.apply_session_directive(
            state,
            slot,
            "dashboard:member-conductor",
            "monitor_start",
            {"message": "patrol", "idle_secs": 1200, "max_cycles": 0},
            producer_is_user_facing=True,
        )

    assert "started on this session" in result
    assert captured["slot_key"] == "member-conductor"
    assert captured["initiator_slot_key"] == "member-conductor"


@pytest.mark.asyncio
async def test_monitor_watch_directive_passes_its_own_binding_as_initiator() -> None:
    captured: dict[str, Any] = {}

    async def _authz(**kw: Any) -> tuple[Any, None, int]:
        captured.update(kw)
        return SimpleNamespace(id="monitor-1"), None, 200

    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _authz),
        patch.object(sda, "_audit"),
    ):
        result = await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="member-conductor", _app="", messages=[]),
            "dashboard:member-conductor",
            "monitor_watch",
            {
                "kind": "github_pull_request",
                "target": "https://github.com/acme/widgets/pull/7",
                "objective": "review_ready",
                "cadence_secs": 300,
                "max_runtime_secs": 14_400,
                "max_agent_turns": 8,
                "max_tokens": 250_000,
                "max_provider_errors": 3,
            },
            producer_is_user_facing=True,
        )

    assert "started on this session" in result
    assert captured["initiator_slot_key"] == "member-conductor"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_start", "monitor_watch"])
async def test_refused_arm_writes_a_notice_row_into_the_session(kind: str) -> None:
    """(c) The MCP tool already said "requested"; a refusal that only reaches
    the log leaves a session that believes it armed a loop. The consumer must
    put a row where the session's reader can see it."""

    async def _refuse(**kw: Any) -> tuple[None, str, int]:
        return None, external_arm_refusal("member"), 409

    surfaced = MagicMock()
    state = SimpleNamespace()
    slot = SimpleNamespace(key="member-conductor", _app="", messages=[])
    args: dict[str, Any] = {"message": "patrol", "idle_secs": 1200}
    if kind == "monitor_watch":
        args = {
            "kind": "github_pull_request",
            "target": "https://github.com/acme/widgets/pull/7",
            "objective": "review_ready",
            "cadence_secs": 300,
            "max_runtime_secs": 14_400,
            "max_agent_turns": 8,
            "max_tokens": 250_000,
            "max_provider_errors": 3,
        }
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _refuse),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
        patch.object(sda, "_audit") as audit,
    ):
        result = await sda.apply_session_directive(
            state, slot, "dashboard:member-conductor", kind, args
        )

    assert result.startswith("Failed to start")
    assert "member-mode" in result
    audit.assert_called_with("dashboard:member-conductor", kind, "denied")
    surfaced.assert_called_once()
    called_state, called_slot, role, text, cls = surfaced.call_args.args
    assert called_state is state and called_slot is slot
    assert role == "notice" and cls == "msg msg-info"
    assert text.startswith(sda.ARM_REFUSAL_NOTICE_PREFIX)
    assert "member-mode sessions do not accept direct automation turns" in text


@pytest.mark.asyncio
async def test_refused_arm_without_a_slot_returns_the_reason_and_writes_no_row() -> None:
    """A channel TurnDriver holds no slot: the returned string is its surface."""

    async def _refuse(**kw: Any) -> tuple[None, str, int]:
        return None, "unknown slot", 404

    surfaced = MagicMock()
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _refuse),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
        patch.object(sda, "_audit"),
    ):
        result = await sda.apply_session_directive(
            SimpleNamespace(), None, "dashboard:chat-1", "monitor_start", {"message": "m"}
        )
    assert "unknown slot" in result
    surfaced.assert_not_called()


@pytest.mark.asyncio
async def test_notice_failure_never_masks_the_denial() -> None:
    async def _refuse(**kw: Any) -> tuple[None, str, int]:
        return None, external_arm_refusal("crew"), 409

    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _refuse),
        patch(
            "kiro_crew.dashboard.state.append_and_surface",
            side_effect=RuntimeError("transcript unavailable"),
        ),
        patch.object(sda, "_audit"),
    ):
        result = await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="chat-1", _app="", messages=[]),
            "dashboard:chat-1",
            "monitor_start",
            {"message": "m"},
        )
    assert result.startswith("Failed to start monitor loop: crew-mode")


# ── fire-time re-check honours the record ──────────────────────────────────


class TestFireTimeModeRecheck:
    """``GatewayOrchestrator._fire_dashboard_nudge`` re-checks the slot mode
    before a structured wake enters the provider. It must let a self-armed
    member loop through and keep refusing a slot that switched into crew mode
    under an externally armed loop."""

    @pytest.fixture(autouse=True)
    def _trust_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default: the keystone-gated record vouches for the loop. Subclasses
        override this fixture to model a missing entry."""
        from kiro_crew import autonudge_selfarm

        monkeypatch.setattr(autonudge_selfarm, "is_recorded_self_arm", lambda _i, _s: True)

    @staticmethod
    def _orchestrator() -> Any:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.slack import gateway as gw

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
            orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
        orch.dashboard_state = SimpleNamespace(
            get_slot=MagicMock(return_value=None),
            push_slots_update=MagicMock(),
            _background_tasks=set(),
            run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
        )
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.remove = AsyncMock()
        orch.autonudge_svc.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
        orch._session_tasks = {}
        return orch

    @staticmethod
    def _slot(mode: str) -> MagicMock:
        slot = MagicMock()
        slot.key = "member-conductor"
        slot.running = False
        slot._in_stage_execution = False
        slot._closing = False
        slot.mode = mode
        slot.memory_mode = "persistent"
        return slot

    @staticmethod
    def _structured(*, self_armed: bool) -> NudgeLoop:
        loop = NudgeLoop(
            id="loop-abc",
            slot_key="member-conductor",
            message="",
            idle_secs=300,
            self_armed=self_armed,
        )
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        return loop

    async def _authorize(self, *, mode: str, self_armed: Any) -> bool:
        """Drive one structured wake; return whether it reached the provider.

        The gateway pre-checks the completion hook BEFORE ``_run_chat`` (a
        revoked claim must not reach prompt-submit hooks), so a refused wake
        never enters the runner at all: the verdict is the dispatch result
        (``UNAVAILABLE``) plus ``_run_chat`` never having been called, and an
        admitted wake is ``DISPATCHED`` with the runner's own final recheck
        also answering True.
        """
        from kiro_crew.monitoring.models import MonitorDispatchResult
        from kiro_crew.slack import gateway as gw

        orch = self._orchestrator()
        live = self._slot(mode)
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        structured = self._structured(self_armed=self_armed)
        spawned: list[asyncio.Task] = []
        runner: dict[str, bool] = {}

        async def _run_chat(*_args: Any, **kwargs: Any) -> None:
            hook = kwargs["monitor_completion"]
            runner["authorized"] = await hook.authorize()
            if runner["authorized"]:
                hook.mark_accepted()

        def _spawn(_state: Any, _slot: Any, coro: Any) -> Any:
            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        sel_mock = MagicMock()
        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch.object(gw, "sel", return_value=sel_mock),
            patch("kiro_crew.dashboard.chat._run_chat", new=_run_chat),
        ):
            result = await orch._fire_dashboard_nudge(structured, "[Monitor wake]")
            for task in spawned:
                await task
        if result is MonitorDispatchResult.DISPATCHED:
            sel_mock.log_tool_invocation.assert_not_called()
            assert runner.get("authorized") is True, "dispatched but the runner recheck refused"
            return True
        assert result is MonitorDispatchResult.UNAVAILABLE, result
        assert "authorized" not in runner, "refused wake must never reach _run_chat"
        live.append.assert_not_called()
        # The boundary decision is audited, never a silent drop.
        denied = [
            c.kwargs
            for c in sel_mock.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert denied and denied[0]["tool_name"] == "monitor_fire"
        assert denied[0]["session_key"] == "member-conductor"
        return False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["crew", "member"])
    async def test_self_armed_member_wake_is_authorized(self, mode: str) -> None:
        assert await self._authorize(mode=mode, self_armed=True) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["crew", "member"])
    async def test_externally_armed_wake_on_a_member_slot_is_still_refused(self, mode: str) -> None:
        assert await self._authorize(mode=mode, self_armed=False) is False


# ── arm outcome reporting: success notice, status in refusal, first wake ─────


@pytest.mark.asyncio
async def test_successful_monitor_start_writes_a_notice_and_names_loop_and_first_wake() -> None:
    """Both channels on success: the applier's ack (which overwrites the
    transcript tool_result row) AND a notice row carry the loop id and the
    first wake time, read off the ARMED record's ``next_due_ts``."""
    armed_due = time.time() + 5

    async def _authz(**kw: Any) -> tuple[Any, None, int]:
        return (
            SimpleNamespace(id="loop-77", monitor=None, gate=False, next_due_ts=armed_due),
            None,
            200,
        )

    surfaced = MagicMock()
    state = SimpleNamespace()
    slot = SimpleNamespace(key="member-conductor", _app="", messages=[])
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _authz),
        patch("kiro_crew.dashboard.state.append_and_surface", surfaced),
        patch.object(sda, "_audit"),
    ):
        result = await sda.apply_session_directive(
            state,
            slot,
            "dashboard:member-conductor",
            "monitor_start",
            {"message": "patrol", "idle_secs": 1200, "max_cycles": 0},
        )

    assert "Monitor loop loop-77 started on this session" in result
    assert "first wake in ~" in result and "UTC)" in result
    surfaced.assert_called_once()
    _state, _slot, role, text, _cls = surfaced.call_args.args
    assert role == "notice"
    assert text.startswith(sda.ARM_SUCCESS_NOTICE_PREFIX)
    assert "loop loop-77" in text and "every 20 min" in text and "no cycle cap" in text
    assert "first wake in ~" in text


@pytest.mark.asyncio
async def test_refused_monitor_start_carries_the_status_code() -> None:
    async def _refuse(**kw: Any) -> tuple[None, str, int]:
        return None, external_arm_refusal("member"), 409

    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _refuse),
        patch("kiro_crew.dashboard.state.append_and_surface", MagicMock()),
        patch.object(sda, "_audit"),
    ):
        result = await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="member-conductor", _app="", messages=[]),
            "dashboard:member-conductor",
            "monitor_start",
            {"message": "patrol"},
        )
    assert result.startswith("Failed to start monitor loop: member-mode")
    assert result.endswith("[status 409]")


# ── Persisted-bit hardening + provenance ratchet ───────────────


def test_load_normalises_a_non_boolean_self_armed_to_false(tmp_path: Path) -> None:
    """The loop store is agent-writable. A forged ``"false"`` is truthy, so it
    must decode to the REFUSING value, exactly as ``gate`` does."""
    store = tmp_path / "autonudge.json"
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "loops": [
                    {
                        "id": "forged01",
                        "slot_key": "member-conductor",
                        "message": "patrol",
                        "idle_secs": 300,
                        "self_armed": "false",
                    },
                    {
                        "id": "honest01",
                        "slot_key": "member-other",
                        "message": "patrol",
                        "idle_secs": 300,
                        "self_armed": True,
                    },
                ],
            }
        )
    )
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        svc._load()
        forged = svc.get_by_slot("member-conductor")
        honest = svc.get_by_slot("member-other")
        assert forged is not None and forged.self_armed is False
        assert honest is not None and honest.self_armed is True
    finally:
        svc.stop()


class TestFireTimeGuardIsNotTruthinessBased(TestFireTimeModeRecheck):
    """Belt and braces with ``_load``: even if a non-boolean reached the guard
    (a record mutated in memory, a future loader regression), only ``is True``
    admits. A truthy string must still refuse."""

    @staticmethod
    def _structured(*, self_armed: Any) -> NudgeLoop:  # type: ignore[override]
        loop = NudgeLoop(id="loop-abc", slot_key="member-conductor", message="", idle_secs=300)
        loop.self_armed = self_armed  # type: ignore[assignment]
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        return loop

    @pytest.mark.asyncio
    @pytest.mark.parametrize("forged", ["false", "true", 1])
    async def test_a_truthy_non_boolean_still_refuses(self, forged: Any) -> None:
        assert await self._authorize(mode="member", self_armed=forged) is False

    # The parent class's two tests run again against this subclass with the
    # widened ``_structured`` -- the bool cases must keep their verdicts.


def test_only_the_session_directive_consumer_passes_initiator_slot_key() -> None:
    """Ratchet: ``is_self_arm`` trusts the string it is handed, so the boundary
    is WHO may hand one over. Exactly one module owns session provenance -- the
    directive consumer, which applies a directive to the exact session whose
    turn produced it. A REST handler or app that "resolves" the target and
    passes it back as initiator would silently disable the crew/member
    injection boundary; this test goes red when such a call site appears."""
    import re

    import kiro_crew

    root = Path(kiro_crew.__file__).resolve().parent
    allowed = {root / "dashboard" / "session_directive_apply.py"}
    definers = {root / "autonudge_authz.py"}
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if "_vendor" in path.parts or path in allowed or path in definers:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r"\binitiator_slot_key\s*=", text):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        "initiator_slot_key may only be supplied by the session-directive consumer; "
        f"new call sites: {offenders}"
    )


# ── Injected turns carry no provenance; trust record is required ────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_start", "monitor_watch"])
async def test_a_headless_turn_in_a_member_session_gets_no_self_arm_provenance(kind: str) -> None:
    """A cron injection, a sub-agent sharing the slot, an app- or nudge-driven
    turn all run IN the member's session without BEING it. Without the
    authenticated-human provenance flag the applier hands the authorizer an
    empty initiator, so the crew/member refusal stands for such turns."""
    captured: dict[str, Any] = {}

    async def _authz(**kw: Any) -> tuple[Any, None, int]:
        captured.update(kw)
        return SimpleNamespace(id="loop-1", monitor=None, gate=False, next_due_ts=0.0), None, 200

    args: dict[str, Any] = {"message": "patrol"}
    if kind == "monitor_watch":
        args = {
            "kind": "github_pull_request",
            "target": "https://github.com/acme/widgets/pull/7",
            "objective": "review_ready",
            "cadence_secs": 300,
            "max_runtime_secs": 14_400,
            "max_agent_turns": 8,
            "max_tokens": 250_000,
            "max_provider_errors": 3,
        }
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _authz),
        patch.object(sda, "_audit"),
    ):
        await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="member-conductor", _app="", messages=[]),
            "dashboard:member-conductor",
            kind,
            args,
        )  # producer_is_user_facing left at its False default
    assert captured["initiator_slot_key"] == ""


@pytest.mark.asyncio
async def test_structured_update_provenance_also_requires_a_human_turn() -> None:
    captured: dict[str, Any] = {}

    async def _authz(**kw: Any) -> tuple[Any, None, int]:
        captured.update(kw)
        return SimpleNamespace(id="monitor-1"), None, 200

    structured_loop = SimpleNamespace(
        id="monitor-1",
        slot_key="member-conductor",
        gate=False,
        monitor=SimpleNamespace(),
    )
    svc = SimpleNamespace(get_by_slot=lambda _k: structured_loop)
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=svc),
        patch("kiro_crew.autonudge.is_structured_monitor_loop", return_value=True),
        patch("kiro_crew.autonudge_authz.authorize_and_update_monitor", _authz),
        patch.object(sda, "_audit"),
    ):
        await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="member-conductor", _app="", messages=[]),
            "dashboard:member-conductor",
            "monitor_update",
            {"patch": {"idle_secs": 600}},
        )
        assert captured["initiator_slot_key"] == ""
        await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="member-conductor", _app="", messages=[]),
            "dashboard:member-conductor",
            "monitor_update",
            {"patch": {"idle_secs": 600}},
            producer_is_user_facing=True,
        )
        assert captured["initiator_slot_key"] == "member-conductor"


@pytest.mark.asyncio
async def test_self_arm_fails_closed_before_the_store_is_touched(
    audits: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A self-armed loop with no trust entry would never be allowed to fire, so
    it must not be reported as armed -- and the record is written BEFORE the
    add, so a failed write denies with nothing added and nothing removed. In
    particular a stopped loop this arm would have displaced survives."""

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("trust root unwritable")

    monkeypatch.setattr(autonudge_authz, "record_self_arm", _boom)
    svc = RecordingSvc()
    svc.remove = AsyncMock()  # type: ignore[attr-defined]
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": _member_slot("member")}),
        slot_key="member-conductor",
        message="patrol",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="mcp-directive",
        initiator_slot_key="member-conductor",
    )
    assert loop is None and status == 503 and "not armed" in (error or "")
    assert svc.added == [], "the store was never touched"
    svc.remove.assert_not_awaited()  # type: ignore[attr-defined]
    assert [e["outcome"] for e in audits][-1] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("add_error", "expected_status"),
    [
        (NudgeAdmissionRefused("session changed"), 409),
        (MonitorUpdateConflict("session already has an automation"), 409),
    ],
)
async def test_a_refused_add_forgets_the_pre_written_trust_entry(
    audits: list[dict[str, Any]],
    trust_record: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    add_error: Exception,
    expected_status: int,
) -> None:
    """The entry is written first; if the add then refuses, the entry must not
    outlive it (an orphan a forged same-id row could reuse after a restart)."""
    forgotten: list[str] = []
    monkeypatch.setattr(autonudge_authz, "forget_self_arm", forgotten.append)
    svc = RecordingSvc()

    async def _refuse(**_kw: Any) -> Any:
        raise add_error

    svc.add = _refuse  # type: ignore[method-assign]
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": _member_slot("member")}),
        slot_key="member-conductor",
        message="patrol",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="mcp-directive",
        initiator_slot_key="member-conductor",
    )
    assert loop is None and status == expected_status and error
    assert len(trust_record) == 1, "the entry was written before the add"
    assert forgotten == [trust_record[0][0]], "and forgotten when the add refused"
    assert [e["outcome"] for e in audits][-1] == "denied"


@pytest.mark.asyncio
async def test_a_successful_add_keeps_the_pre_written_trust_entry(
    audits: list[dict[str, Any]],
    trust_record: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    forgotten: list[str] = []
    monkeypatch.setattr(autonudge_authz, "forget_self_arm", forgotten.append)
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": _member_slot("member")}),
        slot_key="member-conductor",
        message="patrol",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="mcp-directive",
        initiator_slot_key="member-conductor",
    )
    assert error is None and status == 200 and loop is not None
    assert trust_record == [(loop.id, "member-conductor")]
    assert forgotten == []


def test_service_refuses_a_pre_minted_id_already_in_use(tmp_path: Path) -> None:
    """The authorizer's pre-minted id names the loop in the trust record, so a
    collision must be refused rather than silently re-minted -- the record
    would otherwise name the wrong loop."""
    from kiro_crew.autonudge import MonitorUpdateConflict as Conflict

    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        svc._loops["taken001"] = NudgeLoop(id="taken001", slot_key="chat-9-9", message="m")
        assert svc._mint_loop_id("fresh002") == "fresh002"
        assert re.fullmatch(r"[0-9a-f]{8}", svc._mint_loop_id(None))
        with pytest.raises(Conflict):
            svc._mint_loop_id("taken001")
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_self_arm_id_reservation_skips_an_id_a_live_loop_already_holds(
    audits: list[dict[str, Any]],
    trust_record: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The trust record is an upsert keyed by loop id: a candidate that
    collides with a live loop would overwrite that loop's entry and the add's
    conflict refusal would then forget it. The authorizer therefore re-mints
    until the store answers absent, so an existing entry is never touched."""
    taken = "deadbeef"
    minted = iter([taken, "0badf00d"])
    monkeypatch.setattr(
        autonudge_authz.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(minted) + "0" * 24),
    )
    svc = RecordingSvc()
    svc.get_by_id = lambda loop_id: (  # type: ignore[method-assign]
        SimpleNamespace(id=loop_id) if loop_id == taken else None
    )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": _member_slot("member")}),
        slot_key="member-conductor",
        message="patrol",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="mcp-directive",
        initiator_slot_key="member-conductor",
    )
    assert error is None and status == 200 and loop is not None
    assert loop.id == "0badf00d" and trust_record == [("0badf00d", "member-conductor")]


@pytest.mark.asyncio
async def test_self_arm_denies_when_no_free_id_can_be_reserved(
    audits: list[dict[str, Any]],
    trust_record: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svc = RecordingSvc()
    svc.get_by_id = lambda loop_id: SimpleNamespace(id=loop_id)  # type: ignore[method-assign]
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-conductor": _member_slot("member")}),
        slot_key="member-conductor",
        message="patrol",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="mcp-directive",
        initiator_slot_key="member-conductor",
    )
    assert loop is None and status == 503 and "reserve" in (error or "")
    assert trust_record == [] and svc.added == []


# ── the slot's own loop wake is the second admitted producer ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitor_start", "monitor_watch"])
async def test_the_slots_own_wake_turn_may_re_arm_its_loop(kind: str) -> None:
    """(a) A member's loop firing on the member's slot is the member keeping
    itself awake; the arm it issues from inside that cycle is its own act, so
    the consumer hands the authorizer the session's binding as initiator."""
    captured: dict[str, Any] = {}

    async def _authz(**kw: Any) -> tuple[Any, None, int]:
        captured.update(kw)
        return SimpleNamespace(id="loop-1", monitor=None, gate=False, next_due_ts=0.0), None, 200

    args: dict[str, Any] = {"message": "patrol"}
    if kind == "monitor_watch":
        args = {
            "kind": "github_pull_request",
            "target": "https://github.com/acme/widgets/pull/7",
            "objective": "review_ready",
            "cadence_secs": 300,
            "max_runtime_secs": 14_400,
            "max_agent_turns": 8,
            "max_tokens": 250_000,
            "max_provider_errors": 3,
        }
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=object()),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", _authz),
        patch.object(sda, "_audit"),
    ):
        await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="member-conductor", _app="", messages=[]),
            "dashboard:member-conductor",
            kind,
            args,
            producer_is_self_wake=True,  # human flag left False: a nudge turn
        )
    assert captured["initiator_slot_key"] == "member-conductor"


@pytest.mark.asyncio
async def test_the_slots_own_wake_turn_may_revise_its_structured_monitor() -> None:
    captured: dict[str, Any] = {}

    async def _authz(**kw: Any) -> tuple[Any, None, int]:
        captured.update(kw)
        return SimpleNamespace(id="monitor-1"), None, 200

    structured_loop = SimpleNamespace(
        id="monitor-1", slot_key="member-conductor", gate=False, monitor=SimpleNamespace()
    )
    with (
        patch(
            "kiro_crew.autonudge.get_instance",
            return_value=SimpleNamespace(get_by_slot=lambda _k: structured_loop),
        ),
        patch("kiro_crew.autonudge.is_structured_monitor_loop", return_value=True),
        patch("kiro_crew.autonudge_authz.authorize_and_update_monitor", _authz),
        patch.object(sda, "_audit"),
    ):
        await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="member-conductor", _app="", messages=[]),
            "dashboard:member-conductor",
            "monitor_update",
            {"patch": {"idle_secs": 600}},
            producer_is_self_wake=True,
        )
    assert captured["initiator_slot_key"] == "member-conductor"


class TestLegacyLoopUpdateOnAMemberSlotKeepsTheSelfArmRule:
    """A LEGACY (prompt) loop on a crew/member slot is the loop this PR admits,
    and ``monitor_update``'s ``message`` is the instruction every later wake
    executes. Its chokepoint (``authorize_and_update_nudge``) carries no session
    identity, so the crew/member rule is applied by the applier, with the SAME
    provenance the structured twin passes as ``initiator_slot_key``: an outside
    turn (cron, app, sub-agent) never reaches the write, the slot's own human
    turn and its own delivered wake do."""

    @staticmethod
    def _legacy_loop() -> SimpleNamespace:
        return SimpleNamespace(
            id="loop-1",
            slot_key="member-conductor",
            cycle_count=1,
            max_cycles=0,
            active=True,
            stopped_reason="",
            created_ts=time.time(),
        )

    @staticmethod
    def _svc(loop: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(get_by_slot=lambda _k: loop, get_by_id=lambda _i: loop)

    async def _apply(self, mode: str, **provenance: bool) -> tuple[str, dict[str, Any]]:
        captured: dict[str, Any] = {}

        async def _authz(**kw: Any) -> tuple[Any, None, int]:
            captured.update(kw)
            return SimpleNamespace(id="loop-1"), None, 200

        loop = self._legacy_loop()
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=self._svc(loop)),
            patch("kiro_crew.autonudge.is_structured_monitor_loop", return_value=False),
            patch("kiro_crew.autonudge_authz.authorize_and_update_nudge", _authz),
            patch.object(sda, "_audit"),
        ):
            result = await sda.apply_session_directive(
                _state({"member-conductor": _member_slot(mode)}),
                SimpleNamespace(key="member-conductor", _app="", messages=[]),
                "dashboard:member-conductor",
                "monitor_update",
                {"patch": {"message": "new standing orders"}},
                **provenance,
            )
        return result, captured

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["member", "crew"])
    async def test_an_outside_turn_is_refused_before_the_write(self, mode: str) -> None:
        result, captured = await self._apply(mode)  # no provenance mark
        assert captured == {}, "the legacy chokepoint was never reached"
        assert result.startswith("Failed to update monitor loop: ")
        assert result.endswith(external_arm_refusal(mode))

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited_denied(self) -> None:
        with patch.object(sda, "_audit") as audit:
            loop = self._legacy_loop()
            with (
                patch("kiro_crew.autonudge.get_instance", return_value=self._svc(loop)),
                patch("kiro_crew.autonudge.is_structured_monitor_loop", return_value=False),
            ):
                await sda.apply_session_directive(
                    _state({"member-conductor": _member_slot("member")}),
                    SimpleNamespace(key="member-conductor", _app="", messages=[]),
                    "dashboard:member-conductor",
                    "monitor_update",
                    {"patch": {"message": "new standing orders"}},
                )
        audit.assert_called_with("dashboard:member-conductor", "monitor_update", "denied")

    @pytest.mark.asyncio
    async def test_the_slots_own_human_turn_may_revise_it(self) -> None:
        result, captured = await self._apply("member", producer_is_user_facing=True)
        assert captured["loop_id"] == "loop-1"
        assert captured["message"] == "new standing orders"
        assert result.startswith("Monitor loop loop-1 updated on this session")

    @pytest.mark.asyncio
    async def test_the_slots_own_wake_turn_may_revise_it(self) -> None:
        result, captured = await self._apply("member", producer_is_self_wake=True)
        assert captured["loop_id"] == "loop-1"
        assert result.startswith("Monitor loop loop-1 updated on this session")

    @pytest.mark.asyncio
    async def test_an_ordinary_slot_is_untouched_by_the_rule(self) -> None:
        """The gate keys on a positively read crew/member mode; a chat slot's
        outside turn keeps the behaviour it had before this rule existed."""
        result, captured = await self._apply("chat")
        assert captured["loop_id"] == "loop-1"
        assert result.startswith("Monitor loop loop-1 updated on this session")


@pytest.mark.asyncio
async def test_a_self_wake_does_not_unlock_the_user_surface_directives() -> None:
    """The wake mark is self-arm provenance ONLY. ``set_project`` and
    ``reset_conversation`` stay behind the authenticated-human gate: a loop's
    wake must never retarget the slot's project or discard its conversation."""
    with patch.object(sda, "_audit") as audit:
        result = await sda.apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="member-conductor", _app="", messages=[]),
            "dashboard:member-conductor",
            "set_project",
            {"path": "/tmp/elsewhere"},
            producer_is_self_wake=True,
        )
    assert result.startswith("Error: set_project only works from a user-facing session")
    audit.assert_called_with("dashboard:member-conductor", "set_project", "denied")


class TestWakeTurnCarriesTheSelfWakeMark:
    """``_fire_dashboard_nudge`` is the ONLY producer of the mark: the turn it
    starts for a loop's delivered wake runs ``_run_chat`` with
    ``_directive_self_wake=True``, so the consumer can tell a member's own wake
    from a cron or app injection on the same slot (which never carry it)."""

    @pytest.mark.asyncio
    async def test_plain_loop_wake_is_marked(self) -> None:
        from kiro_crew.slack import gateway as gw

        orch = TestFireTimeModeRecheck._orchestrator()
        live = TestFireTimeModeRecheck._slot("")
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        seen: list[dict[str, Any]] = []

        async def _run_chat(*_args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)

        spawned: list[asyncio.Task] = []

        def _spawn(_state: Any, _slot: Any, coro: Any, **_k: Any) -> Any:
            task = asyncio.get_running_loop().create_task(coro)
            spawned.append(task)
            return task

        loop = NudgeLoop(
            id="plain777", slot_key="member-conductor", message="patrol", idle_secs=300
        )
        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch("kiro_crew.dashboard.chat._run_chat", new=_run_chat),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
            for task in spawned:
                await task
        assert seen and seen[0].get("_directive_self_wake") is True
        assert seen[0].get("_directive_user_origin") is False


class TestSelfArmTrustRecord:
    """The keystone-gated record itself, against a temporary data home."""

    @pytest.fixture(autouse=True)
    def _home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        from kiro_crew import autonudge_selfarm

        monkeypatch.setattr(autonudge_selfarm, "data_home", lambda: tmp_path)
        return tmp_path

    def test_record_lives_under_trust_and_round_trips(self, tmp_path: Path) -> None:
        from kiro_crew import autonudge_selfarm as sa

        sa.record_self_arm("abc12345", "member-conductor")
        assert sa.self_arm_record_path() == tmp_path / "trust" / sa.SELF_ARM_RECORD_NAME
        assert sa.self_arm_record_path().exists()
        # The read-modify-write transaction is serialised through a sibling lock.
        assert (tmp_path / "trust" / sa._LOCK_NAME).exists()
        assert sa.is_recorded_self_arm("abc12345", "member-conductor") is True
        # Same id on another slot does not inherit the authorization.
        assert sa.is_recorded_self_arm("abc12345", "chat-1-1") is False
        assert sa.is_recorded_self_arm("nothere", "member-conductor") is False

    def test_record_is_an_upsert_that_preserves_sibling_entries(self) -> None:
        """Two members arming back to back (crew boot) must both stay recorded.

        The write must not prune against a caller-supplied live-id snapshot taken
        outside the lock; an arm whose snapshot predates a sibling's ``svc.add``
        but commits last would prune the sibling's entry, refusing that loop at
        every fire. Only revocation removes entries now.
        """
        from kiro_crew import autonudge_selfarm as sa

        sa.record_self_arm("old00001", "member-a")
        sa.record_self_arm("new00002", "member-b")
        assert sa.is_recorded_self_arm("old00001", "member-a") is True
        assert sa.is_recorded_self_arm("new00002", "member-b") is True
        # Re-arming the same id refreshes it without touching the sibling.
        sa.record_self_arm("old00001", "member-a")
        assert sa.is_recorded_self_arm("new00002", "member-b") is True
        sa.forget_self_arm("new00002")
        assert sa.is_recorded_self_arm("new00002", "member-b") is False
        assert sa.is_recorded_self_arm("old00001", "member-a") is True

    def test_concurrent_self_arms_from_worker_threads_all_survive(self) -> None:
        """The exact race the reviewers named: N arms racing through
        ``asyncio.to_thread`` in arbitrary lock order all end up recorded."""
        from concurrent.futures import ThreadPoolExecutor

        from kiro_crew import autonudge_selfarm as sa

        ids = [f"loop{i:04d}" for i in range(12)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: sa.record_self_arm(i, f"member-{i}"), ids))
        assert all(sa.is_recorded_self_arm(i, f"member-{i}") for i in ids)

    def test_readers_are_total_on_a_malformed_file(self) -> None:
        from kiro_crew import autonudge_selfarm as sa

        path = sa.self_arm_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        assert sa.is_recorded_self_arm("x", "y") is False
        path.write_text(json.dumps({"loops": {"x": "not a dict"}}))
        assert sa.is_recorded_self_arm("x", "y") is False
        sa.forget_self_arm("x")  # never raises


class TestFireTimeGuardRequiresTheTrustRecord(TestFireTimeModeRecheck):
    """A boolean True forged into the agent-writable store has no trust entry
    and must refuse; only bit + record together admit."""

    @pytest.fixture(autouse=True)
    def _trust_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[override]
        from kiro_crew import autonudge_selfarm

        monkeypatch.setattr(autonudge_selfarm, "is_recorded_self_arm", lambda _i, _s: False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["crew", "member"])
    async def test_self_armed_member_wake_is_authorized(self, mode: str) -> None:  # type: ignore[override]
        # Inverted on purpose: bit True but NO trust record refuses.
        assert await self._authorize(mode=mode, self_armed=True) is False


# ── Prompt loops gated too; revocation on removal ──────────────────


class TestPromptLoopsAreGatedAtFireTime:
    """The crew/member boundary applies to EVERY dashboard loop, not only the
    structured/gated ones the completion hook covers: a plain message loop
    that is not self-armed must not deliver into a member slot, and the
    refusal is SEL-audited."""

    @staticmethod
    def _orchestrator() -> Any:
        return TestFireTimeModeRecheck._orchestrator()

    @staticmethod
    def _plain(*, self_armed: bool) -> NudgeLoop:
        return NudgeLoop(
            id="plain001",
            slot_key="member-conductor",
            message="patrol",
            idle_secs=300,
            self_armed=self_armed,
        )

    async def _fire(self, *, self_armed: bool, recorded: bool) -> tuple[Any, MagicMock, Any]:
        from kiro_crew import autonudge_selfarm
        from kiro_crew.slack import gateway as gw

        orch = self._orchestrator()
        live = TestFireTimeModeRecheck._slot("member")
        orch.dashboard_state.get_slot = MagicMock(return_value=live)
        spawn = MagicMock(side_effect=lambda _s, _slot, coro, **_k: (coro.close(), MagicMock())[1])
        sel_mock = MagicMock()
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch.object(gw, "sel", return_value=sel_mock),
            patch.object(autonudge_selfarm, "is_recorded_self_arm", lambda _i, _s: recorded),
            patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()),
        ):
            result = await orch._fire_dashboard_nudge(self._plain(self_armed=self_armed))
        return result, sel_mock, spawn

    @pytest.mark.asyncio
    async def test_externally_armed_prompt_loop_is_refused_and_audited(self) -> None:
        result, sel_mock, spawn = await self._fire(self_armed=False, recorded=False)
        assert result is False
        spawn.assert_not_called()
        denied = [
            c.kwargs
            for c in sel_mock.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert denied and denied[0]["tool_name"] == "monitor_fire"

    @pytest.mark.asyncio
    async def test_bit_without_trust_record_is_refused(self) -> None:
        result, _sel, spawn = await self._fire(self_armed=True, recorded=False)
        assert result is False
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_armed_prompt_loop_fires(self) -> None:
        result, sel_mock, spawn = await self._fire(self_armed=True, recorded=True)
        assert result is True
        spawn.assert_called_once()
        sel_mock.log_tool_invocation.assert_not_called()


def test_removing_any_loop_revokes_its_trust_entry_after_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every removal path funnels through ``remove_sync``. A removed id must not
    keep its authorization -- and the revoke keys on REMOVAL, not on the
    agent-writable ``self_armed`` bit (a forged ``false`` plus a restart would
    otherwise skip it and orphan the keystone entry for an id-reusing forgery).
    But only once the store has COMMITTED the removal: a loop whose durable row
    survives a failed save must keep the entry it needs to fire, or a
    persistence hiccup would silently strand a member's own loop."""
    from kiro_crew import autonudge_selfarm

    revoked: list[str] = []
    monkeypatch.setattr(autonudge_selfarm, "forget_self_arm", revoked.append)
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        armed = NudgeLoop(id="self0001", slot_key="member-a", message="m", self_armed=True)
        plain = NudgeLoop(id="plain002", slot_key="chat-1-1", message="m")
        failing = NudgeLoop(id="self0003", slot_key="member-c", message="m", self_armed=True)
        svc._loops.update({armed.id: armed, plain.id: plain, failing.id: failing})
        saves: list[str] = []
        monkeypatch.setattr(svc, "_save", lambda: saves.append("saved"))
        # No running event loop here, so the sync fallback revokes inline.
        assert svc.remove_sync(plain.id, persist=True, emit=False) is plain
        assert revoked == ["plain002"], "revocation must not trust the store's self_armed bit"
        # persist=False: the caller owns the commit and revokes afterwards.
        assert svc.remove_sync(armed.id, persist=False, emit=False) is armed
        assert revoked == ["plain002"]
        svc._revoke_self_arm_for(armed)
        assert revoked == ["plain002", "self0001"]

        # persist=True with a failing save: the row is still stored -> no revoke.
        def _boom() -> None:
            raise OSError("disk")

        monkeypatch.setattr(svc, "_save", _boom)
        with pytest.raises(OSError):
            svc.remove_sync(failing.id, persist=True, emit=False)
        assert revoked == ["plain002", "self0001"]
        # persist=True with a good save: revoked after the commit.
        svc._loops[failing.id] = failing
        monkeypatch.setattr(svc, "_save", lambda: saves.append("saved"))
        assert svc.remove_sync(failing.id, persist=True, emit=False) is failing
        assert revoked == ["plain002", "self0001", "self0003"]
    finally:
        svc.stop()
