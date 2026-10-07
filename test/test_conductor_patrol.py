"""A conductor's bind arms a default work-ledger patrol when it has no loop.

Pins the four behaviours ``conductor_patrol`` exists for:

* the first bind on a loop-less conductor arms ONE ``watch="work-ledger"`` loop
  through the real authorizer, as an outside arm, so a crew/member slot refuses
  it and no self-arm trust record is written;
* a slot that already holds a loop -- active or stopped and retained -- is left
  alone, so a second bind never stacks a second loop;
* a refused arm is logged at WARNING and the bind still succeeds;
* ``work_ledger_read`` flags each OPEN item ``unpatrolled`` while the conductor
  holds no active loop;
* the conductor's own create-only arm REPLACES an active default patrol instead
  of answering 409, and the tag round-trips through the store.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from skill_script_helpers import load_skill_script

from kiro_crew import autonudge, autonudge_authz, conductor_patrol
from kiro_crew import work_ledger as wl
from kiro_crew.autonudge import AutoNudgeService, MonitorUpdateConflict
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.monitoring.models import MonitorActionCompletion, MonitorActionDisposition

CONDUCTOR = "chat-p-conductor"
WORKER = "chat-p-worker"
WORKER_2 = "chat-p-worker-2"

PATROL_BUDGET = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "goal-conductor"
    / "scripts"
    / "patrol_budget.py"
)


class _Svc:
    """A loop store holding at most one loop per slot, as the real one does."""

    def __init__(self) -> None:
        self.loops: dict[str, Any] = {}
        self.added: list[dict[str, Any]] = []

    def get_by_slot(self, slot_key: str) -> Any:
        return self.loops.get(slot_key)

    def get_by_id(self, loop_id: str) -> Any:
        return next((lp for lp in self.loops.values() if lp.id == loop_id), None)

    def _observes_work_ledger(self, loop: Any) -> bool:
        return getattr(loop, "watch", "") == "work-ledger"

    async def add(self, **kw: Any) -> Any:
        existing = self.loops.get(kw["slot_key"])
        if (
            existing is not None
            and kw.get("replace_existing") is False
            and (existing.active or not kw.get("replace_stopped"))
        ):
            raise autonudge.MonitorUpdateConflict("session already has an automation")
        self.added.append(kw)
        loop = SimpleNamespace(
            id=kw.get("loop_id") or f"loop-{len(self.added)}",
            slot_key=kw["slot_key"],
            idle_secs=kw["idle_secs"],
            max_cycles=kw["max_cycles"],
            monitor=None,
            gate=kw.get("gate", False),
            watch=kw.get("watch", ""),
            active=True,
        )
        self.loops[kw["slot_key"]] = loop
        return loop


class _Slot:
    def __init__(self, *, mode: str = "", memory_mode: str = "persistent", created_by: str = ""):
        self.mode = mode
        self.memory_mode = memory_mode
        self.workspace = "default"
        self._created_by = created_by
        self.running = False
        self.is_closing = False


_SLOTS: dict[str, _Slot] = {}


# Autouse, so it patches through the isolation floor's own MonkeyPatch
# (testing-conventions D11): a test's monkeypatch.undo() never strips it.
@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _SLOTS.clear()

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    _floor_monkeypatch.setattr(routes, "_recognize_session", _recognized)
    _floor_monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    _floor_monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    _floor_monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    _floor_monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    _floor_monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    yield
    _SLOTS.clear()


@pytest.fixture
def svc(monkeypatch) -> _Svc:
    store = _Svc()
    monkeypatch.setattr(autonudge, "get_instance", lambda: store)
    return store


@pytest.fixture
def audits(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    return events


@pytest.fixture
def trust_record(monkeypatch) -> list[tuple[str, str]]:
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        autonudge_authz, "record_self_arm", lambda loop_id, slot: writes.append((loop_id, slot))
    )
    return writes


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        _slots=_SLOTS,
        get_slot=lambda key: _SLOTS.get(key),
        sessions=MagicMock(),
        channel_transports={},
    )


def _req(method: str, path: str, *, body: Any = ..., sk: str) -> web.Request:
    app = web.Application()
    app["state"] = _state()
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _record(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_record(
        _req("POST", "/api/work-ledger/record", body=body, sk=CONDUCTOR)
    )
    return resp.status, json.loads(resp.text)


async def _read() -> dict[str, Any]:
    resp = await routes.api_work_ledger_get(
        _req("GET", "/api/work-ledger?compact=true", sk=CONDUCTOR)
    )
    assert resp.status == 200, resp.text
    return json.loads(resp.text)


async def _create(title: str) -> str:
    status, body = await _record(
        {"action": "create", "title": title, "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    return body["item"]["item_id"]


async def _bind(item_id: str, worker: str) -> dict[str, Any]:
    _SLOTS[worker] = _Slot(created_by=CONDUCTOR)
    status, body = await _record(
        {"action": "bind", "item_id": item_id, "worker_session_key": worker}
    )
    assert status == 200, body
    return body


async def _setup(mode: str = "", memory_mode: str = "persistent") -> str:
    _SLOTS[CONDUCTOR] = _Slot(mode=mode, memory_mode=memory_mode)
    status, body = await _record({"action": "goal", "goal": "drive the fleet", "round": 1})
    assert status == 200, body
    return await _create("item one")


# ── arm on first bind ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_bind_arms_one_work_ledger_patrol(svc, audits):
    item_id = await _setup()
    body = await _bind(item_id, WORKER)

    assert body["patrol"] == conductor_patrol.ARMED
    assert "patrol_note" not in body
    assert len(svc.added) == 1
    armed = svc.added[0]
    assert armed["slot_key"] == CONDUCTOR
    assert armed["watch"] == "work-ledger"
    assert armed["gate"] is True
    assert armed["idle_secs"] == 600
    assert armed["max_cycles"] == 300
    assert armed["max_runtime_secs"] == 86400
    assert armed["replace_existing"] is False
    assert "replace_stopped" not in armed  # the authorizer forwards it only when True
    assert armed["message"] == conductor_patrol.PATROL_MESSAGE
    assert armed["default_patrol"] is True
    assert any(e.get("outcome") == "success" for e in audits)


@pytest.mark.asyncio
async def test_member_conductor_is_refused_and_no_trust_record_is_written(
    svc, audits, trust_record
):
    """The bind route cannot prove the turn is the session's own, so a
    member-mode conductor refuses the arm like any outside arm."""
    item_id = await _setup(mode="member")
    body = await _bind(item_id, WORKER)

    assert body["patrol"] == conductor_patrol.REFUSED
    assert body["patrol_note"] == conductor_patrol.ARM_YOURSELF_NOTE
    assert svc.added == []
    assert trust_record == []
    assert not any(e.get("outcome") == "self_armed" for e in audits)


# ── never stack ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_bind_does_not_arm_a_second_loop(svc, audits):
    item_one = await _setup()
    await _bind(item_one, WORKER)
    item_two = await _create("item two")
    body = await _bind(item_two, WORKER_2)

    assert body["patrol"] == conductor_patrol.EXISTING
    assert len(svc.added) == 1


@pytest.mark.asyncio
async def test_a_stopped_retained_loop_is_left_alone(svc, audits):
    """A person's stop must not be revived by a later bind."""
    item_id = await _setup()
    stopped = SimpleNamespace(id="kept", slot_key=CONDUCTOR, active=False)
    svc.loops[CONDUCTOR] = stopped
    body = await _bind(item_id, WORKER)

    assert body["patrol"] == conductor_patrol.EXISTING
    assert body["patrol_note"] == conductor_patrol.ARM_YOURSELF_NOTE
    assert svc.added == []
    assert svc.loops[CONDUCTOR] is stopped


@pytest.mark.asyncio
async def test_a_person_stopped_default_patrol_is_left_alone(svc, audits):
    item_id = await _setup()
    stopped = SimpleNamespace(
        id="kept",
        slot_key=CONDUCTOR,
        active=False,
        default_patrol=True,
        stopped_reason="",
        monitor=None,
    )
    svc.loops[CONDUCTOR] = stopped
    body = await _bind(item_id, WORKER)

    assert body["patrol"] == conductor_patrol.EXISTING
    assert body["patrol_note"] == conductor_patrol.ARM_YOURSELF_NOTE
    assert svc.loops[CONDUCTOR] is stopped


@pytest.mark.asyncio
async def test_a_system_stopped_default_patrol_is_rearmed_on_bind(svc, audits):
    """Our own default ran out its runtime budget; a new bind is new work."""
    item_id = await _setup()
    svc.loops[CONDUCTOR] = SimpleNamespace(
        id="spent",
        slot_key=CONDUCTOR,
        active=False,
        default_patrol=True,
        stopped_reason="runtime_budget",
        monitor=None,
    )
    body = await _bind(item_id, WORKER)

    assert body["patrol"] == conductor_patrol.ARMED
    assert "patrol_note" not in body
    assert svc.added[0]["replace_stopped"] is True
    assert svc.loops[CONDUCTOR].active is True


@pytest.mark.asyncio
async def test_an_active_loop_that_watches_something_else_gets_the_note(svc, audits):
    item_id = await _setup()
    svc.loops[CONDUCTOR] = SimpleNamespace(id="pr", slot_key=CONDUCTOR, active=True, watch="")
    body = await _bind(item_id, WORKER)

    assert body["patrol"] == conductor_patrol.EXISTING
    assert body["patrol_note"] == conductor_patrol.ARM_YOURSELF_NOTE


# ── a refused arm does not fail the bind ──────────────────────────────────


@pytest.mark.asyncio
async def test_refused_arm_logs_warning_and_bind_still_succeeds(svc, audits, caplog):
    item_id = await _setup(memory_mode="temporary")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.conductor_patrol"):
        body = await _bind(item_id, WORKER)

    assert body["patrol"] == conductor_patrol.REFUSED
    assert body["item"]["worker_session_key"] == WORKER
    assert wl.read_binding(WORKER) is not None
    assert svc.added == []
    assert any("conductor patrol arm refused" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_authorizer_crash_does_not_fail_the_bind(svc, monkeypatch, caplog):
    async def _boom(**kw: Any) -> Any:
        raise RuntimeError("store wedged")

    monkeypatch.setattr(autonudge_authz, "authorize_and_add_nudge", _boom)
    item_id = await _setup()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.conductor_patrol"):
        body = await _bind(item_id, WORKER)

    assert body["patrol"] == conductor_patrol.REFUSED
    assert wl.read_binding(WORKER) is not None


@pytest.mark.asyncio
async def test_disabled_autonudge_reports_unsupported(monkeypatch):
    monkeypatch.setattr(autonudge, "get_instance", lambda: None)
    item_id = await _setup()
    body = await _bind(item_id, WORKER)
    assert body["patrol"] == conductor_patrol.UNSUPPORTED
    assert body["patrol_note"] == conductor_patrol.ARM_YOURSELF_NOTE


# ── the unpatrolled backstop ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_item_is_unpatrolled_until_an_active_work_ledger_watch(svc):
    item_id = await _setup()

    async def _flag() -> bool:
        return {r["item_id"]: r for r in (await _read())["items"]}[item_id]["unpatrolled"]

    assert await _flag() is True
    # A stopped watch patrols nothing.
    svc.loops[CONDUCTOR] = SimpleNamespace(
        id="x", slot_key=CONDUCTOR, active=False, watch="work-ledger"
    )
    assert await _flag() is True
    # An active loop that watches something else (a pull request) reads no ledger.
    svc.loops[CONDUCTOR] = SimpleNamespace(id="x", slot_key=CONDUCTOR, active=True, watch="")
    assert await _flag() is True
    svc.loops[CONDUCTOR].watch = "work-ledger"
    assert await _flag() is False


@pytest.mark.asyncio
async def test_closed_item_is_never_unpatrolled(svc):
    item_id = await _setup()
    status, body = await _record({"action": "close", "item_id": item_id, "state": "abandoned"})
    assert status == 200, body
    rows = {r["item_id"]: r for r in (await _read())["items"]}
    assert rows[item_id]["unpatrolled"] is False


# ── the defaults pass the conductor's own budget check ────────────────────


def test_defaults_pass_patrol_budget_check():
    pb = load_skill_script("patrol_budget_for_conductor_patrol", PATROL_BUDGET)
    _doc, code = pb.check(
        conductor_patrol.PATROL_INTERVAL_SECS,
        conductor_patrol.PATROL_MAX_CYCLES,
        conductor_patrol.PATROL_MAX_RUNTIME_SECS,
    )
    assert code == 0
    assert 300 <= conductor_patrol.PATROL_INTERVAL_SECS <= 900


# ── the conductor's own monitor_start replaces the default ────────────────

SLOT = "chat-p-conductor"


async def _default(svc: AutoNudgeService) -> Any:
    return await svc.add(
        slot_key=SLOT,
        message=conductor_patrol.PATROL_MESSAGE,
        idle_secs=600,
        max_cycles=300,
        watch="work-ledger",
        replace_existing=False,
        default_patrol=True,
    )


async def _directive_arm(svc: AutoNudgeService) -> Any:
    """The create-only shape ``session_directive_apply._monitor_start`` sends."""
    return await svc.add(
        slot_key=SLOT,
        message="my own patrol with my exit condition",
        idle_secs=300,
        max_cycles=50,
        replace_existing=False,
        replace_stopped=True,
    )


@pytest.mark.asyncio
async def test_own_arm_replaces_an_active_default_patrol(tmp_path):
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        default = await _default(svc)
        assert default.default_patrol is True
        assert svc._observes_work_ledger(default) is True
        own = await _directive_arm(svc)
        assert own.default_patrol is False
        assert svc.get_by_slot(SLOT) is own
        assert svc.get_by_id(default.id) is None
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_own_arm_replaces_a_default_patrol_mid_wake(tmp_path):
    """The wake that tells the conductor to arm is the default's own."""
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        default = await _default(svc)
        assert default.monitor is not None
        default.monitor.wake_in_flight = True
        default.monitor.last_wake_fingerprint = "fp-default"
        own = await _directive_arm(svc)
        assert svc.get_by_slot(SLOT) is own
        # The displaced wake's completion is DROPPED: it names a record that is
        # gone, so nothing is charged to the replacement and nothing re-arms.
        await svc.record_monitor_turn_completion(
            MonitorActionCompletion(
                monitor_id=default.id,
                fingerprint="fp-default",
                disposition=MonitorActionDisposition.SUCCESS,
                completed_ts=own.created_ts + 1,
            )
        )
        assert svc.get_by_id(default.id) is None
        assert svc.get_by_slot(SLOT) is own
        assert own.cycle_count == 0 and own.active is True
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_own_arm_still_409s_over_an_ordinary_loop(tmp_path):
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        await svc.add(slot_key=SLOT, message="someone else's loop", idle_secs=300)
        with pytest.raises(MonitorUpdateConflict):
            await _directive_arm(svc)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_default_never_displaces_a_default(tmp_path):
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        await _default(svc)
        with pytest.raises(MonitorUpdateConflict):
            await _default(svc)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_person_stopped_default_patrol_stays_evidence(tmp_path):
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        default = await _default(svc)
        default.active = False
        default.stopped_reason = ""
        with pytest.raises(MonitorUpdateConflict):
            await _directive_arm(svc)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_default_patrol_tag_round_trips_and_a_non_bool_decodes_false(tmp_path):
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        await _default(svc)
    finally:
        svc.stop()
    reloaded = AutoNudgeService(base_dir=tmp_path)
    try:
        reloaded._load()
        stored = reloaded.get_by_slot(SLOT)
        assert stored is not None and stored.default_patrol is True
    finally:
        reloaded.stop()

    store = tmp_path / "autonudge.json"
    doc = json.loads(store.read_text(encoding="utf-8"))
    doc["loops"][0]["default_patrol"] = "true"
    store.write_text(json.dumps(doc), encoding="utf-8")
    forged = AutoNudgeService(base_dir=tmp_path)
    try:
        forged._load()
        stored = forged.get_by_slot(SLOT)
        assert stored is not None and stored.default_patrol is False
    finally:
        forged.stop()


@pytest.mark.asyncio
async def test_real_store_rearms_a_system_stopped_default(tmp_path):
    """The shape ``ensure_patrol`` sends for a spent default: a default over a
    default, admitted only because the system stopped the old one."""
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        old = await _default(svc)
        old.active = False
        old.stopped_reason = "runtime_budget"
        new = await svc.add(
            slot_key=SLOT,
            message=conductor_patrol.PATROL_MESSAGE,
            idle_secs=600,
            max_cycles=300,
            watch="work-ledger",
            replace_existing=False,
            replace_stopped=True,
            default_patrol=True,
        )
        assert svc.get_by_slot(SLOT) is new and new.active is True
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_monitor_watch_replaces_an_active_default_patrol_mid_wake(tmp_path):
    """The structured create-only arm (``monitor_watch``) never 409s on the default."""
    from kiro_crew.monitoring.models import MonitorBudgets

    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        default = await _default(svc)
        assert default.monitor is not None
        default.monitor.wake_in_flight = True
        own = await svc.add_monitor(
            slot_key=SLOT,
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
            replace_existing=False,
            replace_stopped=True,
        )
        assert svc.get_by_slot(SLOT) is own
        assert svc.get_by_id(default.id) is None
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_monitor_watch_still_409s_over_an_ordinary_loop(tmp_path):
    from kiro_crew.monitoring.models import MonitorBudgets

    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        await svc.add(slot_key=SLOT, message="someone else's loop", idle_secs=300)
        with pytest.raises(MonitorUpdateConflict):
            await svc.add_monitor(
                slot_key=SLOT,
                kind="github_pull_request",
                target="owner/repo#123",
                objective="review_ready",
                cadence_secs=60,
                budgets=MonitorBudgets(),
                replace_existing=False,
                replace_stopped=True,
            )
    finally:
        svc.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_replacing_a_firing_default_does_not_cancel_its_running_wake(tmp_path, structured):
    """A channel wake's timer task awaits the whole turn that issues the arm.

    Displacing the default must leave that task running, not cancel it."""
    import asyncio

    from kiro_crew.monitoring.models import MonitorBudgets

    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        default = await _default(svc)
        release = asyncio.Event()

        async def _turn() -> str:
            await release.wait()
            return "turn finished"

        wake = asyncio.ensure_future(_turn())
        svc._cancel_timer(default.id)
        svc._timers[default.id] = wake
        svc._firing.add(default.id)
        if structured:
            own = await svc.add_monitor(
                slot_key=SLOT,
                kind="github_pull_request",
                target="owner/repo#123",
                objective="review_ready",
                cadence_secs=60,
                budgets=MonitorBudgets(),
                replace_existing=False,
                replace_stopped=True,
            )
        else:
            own = await _directive_arm(svc)
        assert svc.get_by_slot(SLOT) is own
        assert not wake.cancelled()
        release.set()
        assert await wake == "turn finished"
        svc._firing.discard(default.id)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_displaced_default_emits_no_fired_frame_after_its_wake(tmp_path):
    """The displaced default's wake still delivers; it must not announce the
    removed row as fired."""
    svc = AutoNudgeService(base_dir=tmp_path)
    events: list[tuple[str, str]] = []
    try:
        default = await _default(svc)
        svc.subscribe(lambda kind, lp: events.append((kind, getattr(lp, "id", ""))))

        async def _fire(loop: Any) -> bool:
            # The turn issues the conductor's own arm, displacing the default.
            await _directive_arm(svc)
            return True

        svc._on_fire = _fire
        await svc._run_fire_cycle(default)
        assert svc.get_by_id(default.id) is None
        assert ("fired", default.id) not in events
    finally:
        svc.stop()
