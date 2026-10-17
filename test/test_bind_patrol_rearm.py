"""Every bind leaves the conductor with a work-ledger patrol, or says why not.

The bind route arms a default ``watch="work-ledger"`` patrol after a bind. These
tests pin the cases a dispatch meets after the conductor's own loop ended:

* a loop the SYSTEM stopped (its subject went terminal, a bound ran out) counts
  as no loop, whoever armed it, and is replaced;
* a loop a PERSON stopped is kept, and the reply says ``refused`` with reason
  ``user-stopped``;
* an active loop is left alone;
* the MCP reply always carries the patrol outcome and its reason.

The arm path reads the slot key, its mode and its memory mode; it never reads
which backend runs the session, so one dashboard slot shape covers every
backend. A crew/member slot is the other shape: it refuses an outside arm, and
the reply must say so.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import autonudge, autonudge_authz, conductor_patrol, mcp_core, mcp_work
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.monitoring.models import MonitorOutcome

WORKER = "chat-r-worker"
DASHBOARD = "chat-r-conductor"


class _Svc:
    """One loop per slot, admitting a create-only arm the way the real store does."""

    def __init__(self) -> None:
        self.loops: dict[str, Any] = {}
        self.added: list[dict[str, Any]] = []

    def get_by_slot(self, slot_key: str) -> Any:
        return self.loops.get(slot_key)

    def _observes_work_ledger(self, loop: Any) -> bool:
        return getattr(loop, "watch", "") == "work-ledger"

    async def add(self, **kw: Any) -> Any:
        existing = self.loops.get(kw["slot_key"])
        if existing is not None and (existing.active or not kw.get("replace_stopped")):
            raise autonudge.MonitorUpdateConflict("session already has an automation")
        self.added.append(kw)
        loop = SimpleNamespace(
            id=f"loop-{len(self.added)}",
            slot_key=kw["slot_key"],
            watch=kw.get("watch", ""),
            active=True,
            default_patrol=kw.get("default_patrol", False),
        )
        self.loops[kw["slot_key"]] = loop
        return loop


class _Slot:
    def __init__(self, *, created_by: str = "", mode: str = "") -> None:
        self.mode = mode
        self.memory_mode = "persistent"
        self.workspace = "default"
        self._created_by = created_by
        self.running = False
        self.is_closing = False


_SLOTS: dict[str, _Slot] = {}


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
    _floor_monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None),
    )
    yield
    _SLOTS.clear()


@pytest.fixture
def svc(monkeypatch) -> _Svc:
    store = _Svc()
    monkeypatch.setattr(autonudge, "get_instance", lambda: store)
    return store


@pytest.fixture
def conductor() -> str:
    return DASHBOARD


def _req(body: Any, sk: str) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(
        _slots=_SLOTS,
        get_slot=lambda key: _SLOTS.get(key),
        sessions=MagicMock(),
        channel_transports={},
    )
    req = make_mocked_request(
        "POST", "/api/work-ledger/record", app=app, headers={"X-Session-Key": sk}
    )
    req["internal_auth"] = True
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _record(conductor: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = await routes.api_work_ledger_record(_req(body, conductor))
    assert resp.status == 200, resp.text
    return json.loads(resp.text)


async def _bind_one(conductor: str, *, mode: str = "") -> dict[str, Any]:
    _SLOTS[conductor] = _Slot(mode=mode)
    await _record(conductor, {"action": "goal", "goal": "drive", "round": 1})
    created = await _record(
        conductor,
        {"action": "create", "title": "one", "acceptance": {"kind": "human_approval"}},
    )
    _SLOTS[WORKER] = _Slot(created_by=conductor)
    return await _record(
        conductor,
        {
            "action": "bind",
            "item_id": created["item"]["item_id"],
            "worker_session_key": WORKER,
        },
    )


def _slot_of(conductor: str) -> str:
    return conductor


def _own_loop_finished(slot: str) -> SimpleNamespace:
    """The conductor's OWN monitor_start loop, stopped because every item went terminal."""
    return SimpleNamespace(
        id="own",
        slot_key=slot,
        active=False,
        watch="work-ledger",
        default_patrol=False,
        stopped_reason="monitor_terminal",
        monitor=SimpleNamespace(outcome=MonitorOutcome.SUCCESS, stopped_reason="monitor_terminal"),
    )


@pytest.mark.asyncio
async def test_a_system_stopped_own_loop_is_replaced_on_bind(svc, conductor):
    slot = _slot_of(conductor)
    svc.loops[slot] = _own_loop_finished(slot)

    body = await _bind_one(conductor)

    assert body["patrol"] == conductor_patrol.ARMED
    assert body["patrol_reason"] == "replaced-system-stopped"
    assert "patrol_note" not in body
    assert svc.added[0]["replace_stopped"] is True
    assert svc.loops[slot].active is True
    assert svc.loops[slot].watch == "work-ledger"


@pytest.mark.asyncio
async def test_a_finished_pr_watch_is_replaced_on_bind(svc, conductor):
    """A slot holds one loop. A PR watch that ended because its PR merged has
    nothing left to watch, and the new bind is work nobody reads without a
    patrol, so the finished watch is replaced on purpose."""
    slot = _slot_of(conductor)
    finished = _own_loop_finished(slot)
    finished.watch = ""
    svc.loops[slot] = finished

    body = await _bind_one(conductor)

    assert body["patrol"] == conductor_patrol.ARMED
    assert body["patrol_reason"] == conductor_patrol.REASON_REPLACED
    assert svc.loops[slot].watch == "work-ledger"


@pytest.mark.asyncio
async def test_holds_ledger_reads_the_callers_own_ledger(svc, conductor):
    assert await routes.holds_ledger(conductor) is False
    await _bind_one(conductor)
    assert await routes.holds_ledger(conductor) is True
    assert await routes.holds_ledger("chat-r-nobody") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [MonitorOutcome.USER_STOP, None])
async def test_a_user_stopped_loop_is_refused_not_rearmed(svc, conductor, outcome):
    slot = _slot_of(conductor)
    kept = SimpleNamespace(
        id="kept",
        slot_key=slot,
        active=False,
        watch="work-ledger",
        default_patrol=False,
        stopped_reason="manual",
        monitor=(
            SimpleNamespace(outcome=outcome, stopped_reason="") if outcome is not None else None
        ),
    )
    svc.loops[slot] = kept

    body = await _bind_one(conductor)

    assert body["patrol"] == conductor_patrol.REFUSED
    assert body["patrol_reason"] == "user-stopped"
    assert body["patrol_note"] == conductor_patrol.ARM_YOURSELF_NOTE
    assert svc.added == []
    assert svc.loops[slot] is kept


@pytest.mark.asyncio
async def test_an_active_loop_is_left_alone(svc, conductor):
    slot = _slot_of(conductor)
    live = SimpleNamespace(id="live", slot_key=slot, active=True, watch="work-ledger")
    svc.loops[slot] = live

    body = await _bind_one(conductor)

    assert body["patrol"] == conductor_patrol.EXISTING
    assert body["patrol_reason"] == "active-work-ledger"
    assert "patrol_note" not in body
    assert svc.added == []
    assert svc.loops[slot] is live


@pytest.mark.asyncio
async def test_no_loop_arms_one(svc, conductor):
    body = await _bind_one(conductor)

    assert body["patrol"] == conductor_patrol.ARMED
    assert body["patrol_reason"] == "no-loop"
    assert svc.loops[_slot_of(conductor)].watch == "work-ledger"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["member", "crew"])
async def test_a_member_slot_refuses_the_outside_arm_and_says_why(svc, conductor, mode):
    """The bind route cannot prove the turn is the slot's own, so a crew/member
    slot keeps refusing the arm; the reply names that and tells it to self-arm."""
    slot = _slot_of(conductor)
    svc.loops[slot] = _own_loop_finished(slot)

    body = await _bind_one(conductor, mode=mode)

    assert body["patrol"] == conductor_patrol.REFUSED
    assert body["patrol_reason"] == "outside-arm-refused"
    assert body["patrol_note"] == conductor_patrol.ARM_YOURSELF_NOTE
    assert svc.added == []


@pytest.mark.asyncio
async def test_real_store_admits_a_default_over_a_finished_own_loop(tmp_path):
    """The arm ``ensure_patrol`` sends lands on the real store over the
    conductor's own finished loop, not only on the fake."""
    svc = AutoNudgeService(base_dir=tmp_path)
    try:
        own = await svc.add(
            slot_key=DASHBOARD,
            message="my own patrol",
            idle_secs=600,
            watch="work-ledger",
            replace_existing=False,
        )
        own.active = False
        own.stopped_reason = "monitor_terminal"
        assert conductor_patrol._stopped_row_is_replaceable(own) is True
        new = await svc.add(
            slot_key=DASHBOARD,
            message=conductor_patrol.PATROL_MESSAGE,
            idle_secs=600,
            max_cycles=300,
            watch="work-ledger",
            replace_existing=False,
            replace_stopped=True,
            default_patrol=True,
        )
        assert svc.get_by_slot(DASHBOARD) is new and new.active is True
    finally:
        svc.stop()


# ── the MCP reply ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("patrol", "reason", "note"),
    [
        ("armed", "replaced-system-stopped", None),
        ("existing", "active-work-ledger", None),
        ("refused", "user-stopped", conductor_patrol.ARM_YOURSELF_NOTE),
        ("unsupported", "autonudge-disabled", conductor_patrol.ARM_YOURSELF_NOTE),
    ],
)
def test_the_bind_reply_always_carries_the_patrol_outcome(monkeypatch, patrol, reason, note):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    resp: dict[str, Any] = {
        "ok": True,
        "action": "bind",
        "item": {"item_id": "it_00000009", "state": "open"},
        "patrol": patrol,
        "patrol_reason": reason,
    }
    if note:
        resp["patrol_note"] = note
    monkeypatch.setattr(mcp_work, "_post", lambda *a, **k: resp)

    out = mcp_work._call_tool_inner(
        "work_ledger_record",
        {"action": "bind", "item_id": "it_00000009", "worker_session_key": WORKER},
    )

    assert f"patrol={patrol} ({reason})" in out
    if note:
        assert note in out
    else:
        assert "No patrol runs" not in out


def test_a_bind_reply_without_a_patrol_field_says_so(monkeypatch):
    """A route that omits the field is a defect the conductor must see, not a silence."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(
        mcp_work,
        "_post",
        lambda *a, **k: {"ok": True, "action": "bind", "item": {"item_id": "it_1"}},
    )
    out = mcp_work._call_tool_inner(
        "work_ledger_record", {"action": "bind", "item_id": "it_1", "worker_session_key": WORKER}
    )
    assert "patrol=unknown" in out
    assert conductor_patrol.ARM_YOURSELF_NOTE in out
