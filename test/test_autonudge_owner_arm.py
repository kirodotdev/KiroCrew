"""Perpetual mode: the dashboard OWNER arms a member's own loop from the Crew Members page.

A member-mode slot refuses every arm that did not come from its own turn. The
owner's Perpetual mode switch is the second admitted party: it is granted only
on the owner-gated ``POST /api/members/{slug}/perpetual`` route, recorded in
the keystone-gated trust record under its own ``armed_by``, and honoured by the
fire-time guard for MEMBER slots only. It never claims to be a self-arm.

Pinned here:

(a) TRUST RECORD -- an owner entry and a self entry are disjoint: each reader
    vouches for exactly one party, and an entry without ``armed_by`` reads as
    self (the only writer that existed before the field).
(b) AUTHORIZER -- ``owner_arm=True`` admits a member slot with an
    ``owner_armed`` audit, writes the owner record before the add, never sets
    ``self_armed``, is refused on a crew slot, and is a no-op flag on an
    ordinary slot. Without the flag a member slot is refused exactly as before.
(c) FIRE GUARD -- a member wake with a recorded owner arm is admitted; a crew
    wake is not; a forged ``self_armed`` bit cannot ride an owner entry.
(d) ROUTE -- owner-only; slot key derived from the binding; ON arms with
    unlimited cycles and runtime, ON on a stopped loop resumes it and lifts its
    caps, OFF pauses (record kept, reason ``manual``), a structured monitor is
    never converted, a finite loop elsewhere is untouched.

Every test patches ``sel`` so nothing is written to the real security log, and
the trust record against a temporary data home.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import autonudge_authz
from kiro_crew import autonudge_selfarm as sa
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.autonudge_authz import authorize_and_add_nudge

# ── fixtures ────────────────────────────────────────────────────────────────


class RecordingSvc:
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
            max_runtime_secs=kw.get("max_runtime_secs", 0),
            monitor=None,
            gate=kw.get("gate", False),
        )


def _state(slots: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(_slots=slots, sessions=None, channel_transports={})


def _slot(mode: str) -> SimpleNamespace:
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
def trust_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(sa, "data_home", lambda: tmp_path)
    return tmp_path


# ── (a) the trust record ────────────────────────────────────────────────────


class TestTrustRecordParties:
    def test_owner_and_self_entries_are_disjoint(self, trust_home: Path) -> None:
        sa.record_owner_arm("own00001", "member-scout")
        sa.record_self_arm("slf00001", "member-scout")
        assert sa.is_recorded_owner_arm("own00001", "member-scout") is True
        assert sa.is_recorded_self_arm("own00001", "member-scout") is False
        assert sa.is_recorded_self_arm("slf00001", "member-scout") is True
        assert sa.is_recorded_owner_arm("slf00001", "member-scout") is False
        # Slot must match for either party.
        assert sa.is_recorded_owner_arm("own00001", "member-other") is False

    def test_entry_without_armed_by_reads_as_self(self, trust_home: Path) -> None:
        sa.record_self_arm("old00001", "member-a")
        raw = sa.self_arm_record_path().read_text(encoding="utf-8")
        assert '"armed_by": "self"' in raw
        # Simulate a record written before the field existed.
        stripped = raw.replace(', "armed_by": "self"', "").replace('"armed_by": "self", ', "")
        sa.self_arm_record_path().write_text(stripped, encoding="utf-8")
        assert "armed_by" not in sa.self_arm_record_path().read_text(encoding="utf-8")
        assert sa.is_recorded_self_arm("old00001", "member-a") is True
        assert sa.is_recorded_owner_arm("old00001", "member-a") is False

    def test_unknown_party_vouches_for_nobody(self, trust_home: Path) -> None:
        sa.record_owner_arm("x0000001", "member-a")
        path = sa.self_arm_record_path()
        path.write_text(
            path.read_text(encoding="utf-8").replace('"owner"', '"someone"'), encoding="utf-8"
        )
        assert sa.is_recorded_owner_arm("x0000001", "member-a") is False
        assert sa.is_recorded_self_arm("x0000001", "member-a") is False

    def test_forget_revokes_an_owner_entry_too(self, trust_home: Path) -> None:
        sa.record_owner_arm("own00002", "member-a")
        sa.forget_self_arm("own00002")
        assert sa.is_recorded_owner_arm("own00002", "member-a") is False


# ── (b) the authorizer ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_arm_admits_a_member_slot_without_claiming_self_arm(
    audits: list[dict[str, Any]], trust_home: Path, tmp_path: Path
) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-scout": _slot("member")}),
        slot_key="member-scout",
        message="perpetual",
        max_cycles=0,
        max_runtime_secs=0,
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
        caller="127.0.0.1",
        replace_existing=False,
        owner_arm=True,
    )
    assert error is None and status == 200 and loop is not None
    assert len(svc.added) == 1
    added = svc.added[0]
    # The store record never carries the self-arm bit: the owner is a party of
    # its own, and the fire guard reads the trust record for it.
    assert "self_armed" not in added
    assert added["max_cycles"] == 0 and added["max_runtime_secs"] == 0
    # The owner record was written, under the pre-minted id, on this slot ...
    assert sa.is_recorded_owner_arm(added["loop_id"], "member-scout") is True
    # ... and it is NOT a self-arm entry.
    assert sa.is_recorded_self_arm(added["loop_id"], "member-scout") is False
    outcomes = [event["outcome"] for event in audits]
    assert "owner_armed" in outcomes and "self_armed" not in outcomes
    invoked = next(event for event in audits if event["outcome"] == "invoked")
    assert invoked["metadata"]["owner_armed"] is True
    assert invoked["metadata"]["self_armed"] is False


@pytest.mark.asyncio
async def test_owner_arm_is_refused_on_a_crew_slot(
    audits: list[dict[str, Any]], trust_home: Path, tmp_path: Path
) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"crew-1": _slot("crew")}),
        slot_key="crew-1",
        message="perpetual",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
        owner_arm=True,
    )
    assert loop is None and status == 409
    assert error is not None and error.startswith("crew-mode sessions do not accept")
    assert svc.added == []
    assert not sa.self_arm_record_path().exists()


@pytest.mark.asyncio
async def test_member_slot_without_owner_arm_is_refused_as_before(
    audits: list[dict[str, Any]], trust_home: Path, tmp_path: Path
) -> None:
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-scout": _slot("member")}),
        slot_key="member-scout",
        message="perpetual",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
    )
    assert loop is None and status == 409
    assert error is not None and error.startswith("member-mode sessions do not accept")
    assert svc.added == []


@pytest.mark.asyncio
async def test_owner_arm_flag_is_inert_on_an_ordinary_slot(
    audits: list[dict[str, Any]], trust_home: Path, tmp_path: Path
) -> None:
    """A plain chat slot admits every arm anyway; the flag must not write a
    trust entry for it or mark the audit as an owner arm."""
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"chat-1-1": _slot("")}),
        slot_key="chat-1-1",
        message="goal",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
        owner_arm=True,
    )
    assert error is None and status == 200 and loop is not None
    assert "loop_id" not in svc.added[0]
    assert not sa.self_arm_record_path().exists()
    invoked = next(event for event in audits if event["outcome"] == "invoked")
    assert invoked["metadata"]["owner_armed"] is False


@pytest.mark.asyncio
async def test_owner_record_write_failure_denies_and_arms_nothing(
    audits: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_loop_id: str, _slot_key: str) -> None:
        raise OSError("trust root unavailable")

    monkeypatch.setattr(autonudge_authz, "record_owner_arm", _boom)
    svc = RecordingSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-scout": _slot("member")}),
        slot_key="member-scout",
        message="perpetual",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
        owner_arm=True,
    )
    assert loop is None and status == 503
    assert error is not None and "owner-arm record unavailable" in error
    assert svc.added == []


@pytest.mark.asyncio
async def test_owner_entry_is_forgotten_when_the_add_conflicts(
    audits: list[dict[str, Any]], trust_home: Path, tmp_path: Path
) -> None:
    from kiro_crew.autonudge import MonitorUpdateConflict

    class ConflictSvc(RecordingSvc):
        async def add(self, **kw: Any) -> Any:
            self.added.append(kw)
            raise MonitorUpdateConflict("session already has an automation")

    svc = ConflictSvc()
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=_state({"member-scout": _slot("member")}),
        slot_key="member-scout",
        message="perpetual",
        stop_sentinel_path=str(tmp_path / "stop"),
        source="dashboard",
        replace_existing=False,
        owner_arm=True,
    )
    assert loop is None and status == 409
    reserved = svc.added[0]["loop_id"]
    assert sa.is_recorded_owner_arm(reserved, "member-scout") is False


# ── (c) the fire-time guard ─────────────────────────────────────────────────


class TestFireGuardOwnerParty:
    @staticmethod
    def _admits(loop: NudgeLoop, mode: str) -> Any:
        from kiro_crew.slack import gateway as gw

        return gw.GatewayOrchestrator._dashboard_mode_admits(loop, SimpleNamespace(mode=mode))

    @pytest.mark.asyncio
    async def test_member_wake_with_owner_record_is_admitted(self, trust_home: Path) -> None:
        sa.record_owner_arm("own00009", "member-scout")
        loop = NudgeLoop(id="own00009", slot_key="member-scout", message="m", idle_secs=60)
        assert loop.self_armed is False
        assert await self._admits(loop, "member") is True

    @pytest.mark.asyncio
    async def test_crew_wake_never_admits_an_owner_record(self, trust_home: Path) -> None:
        sa.record_owner_arm("own00010", "crew-1")
        loop = NudgeLoop(id="own00010", slot_key="crew-1", message="m", idle_secs=60)
        assert await self._admits(loop, "crew") is False

    @pytest.mark.asyncio
    async def test_forged_self_bit_cannot_ride_an_owner_entry(self, trust_home: Path) -> None:
        sa.record_owner_arm("own00011", "member-scout")
        loop = NudgeLoop(
            id="own00011", slot_key="member-scout", message="m", idle_secs=60, self_armed=True
        )
        # Self path refuses (no self entry); owner path admits on its own
        # record -- and on a MEMBER slot only, which is the same answer the
        # honest record gives. The forged bit buys nothing.
        assert await self._admits(loop, "member") is True
        assert await self._admits(loop, "crew") is False

    @pytest.mark.asyncio
    async def test_member_wake_without_any_record_is_refused(self, trust_home: Path) -> None:
        loop = NudgeLoop(id="none0001", slot_key="member-scout", message="m", idle_secs=60)
        assert await self._admits(loop, "member") is False

    @pytest.mark.asyncio
    async def test_self_armed_path_is_unchanged(self, trust_home: Path) -> None:
        sa.record_self_arm("slf00009", "member-scout")
        loop = NudgeLoop(
            id="slf00009", slot_key="member-scout", message="m", idle_secs=60, self_armed=True
        )
        assert await self._admits(loop, "member") is True
        assert await self._admits(loop, "crew") is True


# ── (d) the route ───────────────────────────────────────────────────────────

CREW = "scout"


class FakeLoopSvc:
    """One member loop, mutated the way ``AutoNudgeService.update`` records it."""

    def __init__(self, loop: NudgeLoop | None = None) -> None:
        self.loop = loop
        self.updates: list[dict[str, Any]] = []
        self.added: list[dict[str, Any]] = []

    def get_by_slot(self, slot_key: str) -> Any:
        return self.loop if self.loop and self.loop.slot_key == slot_key else None

    def get_by_id(self, loop_id: str) -> Any:
        return self.loop if self.loop and self.loop.id == loop_id else None

    async def update(self, loop_id: str, **kw: Any) -> Any:
        self.updates.append({"loop_id": loop_id, **kw})
        assert self.loop is not None and self.loop.id == loop_id
        if kw.get("active") is not None:
            was_active = self.loop.active
            self.loop.active = bool(kw["active"])
            if self.loop.active:
                self.loop.stopped_reason = ""
                self.loop.stopped_detail = ""
                if not was_active:
                    self.loop.next_due_ts = 4_000.0
            else:
                self.loop.stopped_reason = kw.get("stopped_reason") or "manual"
                self.loop.stopped_detail = str(kw.get("stopped_detail") or "")
                self.loop.next_due_ts = 0.0
        if kw.get("max_cycles") is not None:
            self.loop.max_cycles = int(kw["max_cycles"])
        if kw.get("max_runtime_secs") is not None:
            self.loop.max_runtime_secs = int(kw["max_runtime_secs"])
        return self.loop

    async def add(self, **kw: Any) -> Any:
        self.added.append(kw)
        self.loop = NudgeLoop(
            id=kw.get("loop_id") or "new00001",
            slot_key=kw["slot_key"],
            message=kw["message"],
            idle_secs=kw["idle_secs"],
            max_cycles=kw["max_cycles"],
            max_runtime_secs=kw.get("max_runtime_secs", 0),
            banner=kw.get("banner", ""),
            next_due_ts=4_000.0,
        )
        return self.loop


def _fake_config() -> Any:
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agents = {CREW: KiroCrewAgentConfig(kiro_agent="kirocrew-autofix")}
    return cfg


def _make_app(slots: dict[str, Any]) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_member_perpetual_set

    @web.middleware
    async def _auth(request: web.Request, handler: Any) -> Any:
        if "app" not in request:
            request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = SimpleNamespace(_slots=slots, sessions=None, channel_transports={})
    app.router.add_post("/api/members/{slug}/perpetual", api_member_perpetual_set)
    return app


def _route_patches(
    svc: Any,
    *,
    binding_slot: str | None = "member-scout",
    binding_member: str = CREW,
    derived_slot: str = "member-scout",
) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    # The thread route's own generation helper, answering the CURRENT derived
    # key for this member; a binding that names another key is stale.
    stack.enter_context(
        patch(
            "kiro_crew.dashboard.handlers.members._member_thread_slot",
            return_value=(derived_slot, ""),
        )
    )
    stack.enter_context(
        patch(
            "kiro_crew.dashboard.handlers.members.require_owner_dashboard_request",
            new=AsyncMock(return_value=None),
        )
    )
    stack.enter_context(
        patch(
            "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
            return_value=_fake_config(),
        )
    )
    stack.enter_context(
        patch(
            "kiro_crew.dashboard.handlers.members.members_mod.read_dm_binding",
            return_value=(
                {"member": binding_member, "slot_key": binding_slot}
                if binding_slot is not None
                else None
            ),
        )
    )
    stack.enter_context(patch("kiro_crew.autonudge.get_instance", return_value=svc))
    # Keep the auto-defaulted sentinel out of the real workspace dir.
    stack.enter_context(
        patch(
            "kiro_crew.autonudge_authz.resolve_stop_sentinel",
            lambda slot_key, workspace="default": str(
                Path(tempfile.gettempdir()) / f".perpetual-test-stop-{slot_key}"
            ),
        )
    )
    return stack


@pytest.fixture
def quiet_authz_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        autonudge_authz,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    return events


def _member_slot_obj(agent: str = CREW) -> SimpleNamespace:
    return SimpleNamespace(
        workspace="default", mode="member", memory_mode="persistent", agent=agent, _app=""
    )


class TestPerpetualRoute:
    @pytest.mark.asyncio
    async def test_non_owner_is_refused_before_anything_else(self) -> None:
        refusal = web.json_response({"error": "forbidden"}, status=403)
        svc = FakeLoopSvc()
        with patch(
            "kiro_crew.dashboard.handlers.members.require_owner_dashboard_request",
            new=AsyncMock(return_value=refusal),
        ):
            async with TestClient(TestServer(_make_app({}))) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 403
        assert svc.added == [] and svc.updates == []

    @pytest.mark.asyncio
    async def test_app_token_gets_existence_hiding_404(self) -> None:
        with patch(
            "kiro_crew.dashboard.handlers.members._sel",
            return_value=SimpleNamespace(log_api_access=lambda **kw: None),
        ):
            async with TestClient(TestServer(_make_app({}))) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual",
                    json={"member": CREW, "enabled": True},
                    headers={"X-Test-App": "some-app"},
                )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_on_with_no_loop_arms_unlimited_owner_loop(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        svc = FakeLoopSvc()
        slots = {"member-scout": _member_slot_obj()}
        with _route_patches(svc):
            async with TestClient(TestServer(_make_app(slots))) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["ok"] is True
        assert body["loop"]["slot_key"] == "member-scout"
        assert body["loop"]["max_cycles"] == 0
        assert body["loop"]["max_runtime_secs"] == 0
        assert body["loop"]["active"] is True
        added = svc.added[0]
        assert added["max_cycles"] == 0 and added["max_runtime_secs"] == 0
        assert added["replace_existing"] is False
        assert "self_armed" not in added
        assert sa.is_recorded_owner_arm(added["loop_id"], "member-scout") is True

    @pytest.mark.asyncio
    async def test_on_with_a_stopped_loop_resumes_it_and_lifts_its_caps(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        stopped = NudgeLoop(
            id="old00001",
            slot_key="member-scout",
            message="patrol",
            idle_secs=900,
            max_cycles=24,
            cycle_count=7,
            active=False,
            stopped_reason="manual",
            max_runtime_secs=14_400,
        )
        svc = FakeLoopSvc(stopped)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["loop"]["id"] == "old00001"
        assert body["loop"]["active"] is True
        assert body["loop"]["max_cycles"] == 0
        assert body["loop"]["max_runtime_secs"] == 0
        # Resumed, not replaced: the cycle accounting and instruction survive.
        assert body["loop"]["cycle_count"] == 7
        assert body["loop"]["message"] == "patrol"
        assert svc.added == []
        assert svc.updates[0]["active"] is True
        # The takeover recorded the owner as the loop's party.
        assert sa.is_recorded_owner_arm("old00001", "member-scout") is True

    @pytest.mark.asyncio
    async def test_off_pauses_and_keeps_the_record_with_its_reason(
        self, quiet_authz_audit: list[dict[str, Any]]
    ) -> None:
        running = NudgeLoop(
            id="run00001",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            cycle_count=3,
            active=True,
            next_due_ts=9_999.0,
        )
        svc = FakeLoopSvc(running)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": False}
                )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["loop"]["active"] is False
        assert body["loop"]["stopped_reason"] == "manual"
        # The pending wake is gone; the record is not.
        assert body["loop"]["next_due_ts"] == 0
        assert svc.loop is running
        assert svc.updates == [
            {
                "loop_id": "run00001",
                "message": None,
                "idle_secs": None,
                "max_cycles": None,
                "active": False,
                "max_runtime_secs": None,
                "banner": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_off_with_no_loop_is_a_no_op(
        self, quiet_authz_audit: list[dict[str, Any]]
    ) -> None:
        svc = FakeLoopSvc()
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": False}
                )
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "loop": None}
        assert svc.updates == [] and svc.added == []

    @pytest.mark.asyncio
    async def test_on_when_already_active_changes_nothing(
        self, quiet_authz_audit: list[dict[str, Any]]
    ) -> None:
        """A finite loop the member armed itself stays finite: the switch is
        already ON for it, and ON again is not a request to lift its caps."""
        finite = NudgeLoop(
            id="fin00001",
            slot_key="member-scout",
            message="m",
            idle_secs=600,
            max_cycles=12,
            max_runtime_secs=7_200,
            active=True,
            self_armed=True,
        )
        svc = FakeLoopSvc(finite)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 200
        body = await resp.json()
        assert body["loop"]["max_cycles"] == 12 and body["loop"]["max_runtime_secs"] == 7_200
        assert svc.updates == [] and svc.added == []

    @pytest.mark.asyncio
    async def test_structured_monitor_is_never_converted(
        self, quiet_authz_audit: list[dict[str, Any]]
    ) -> None:
        from kiro_crew.monitoring.models import MonitorBudgets, MonitorState

        loop = NudgeLoop(id="mon00001", slot_key="member-scout", message="", idle_secs=300)
        loop.monitor = MonitorState(
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
        )
        svc = FakeLoopSvc(loop)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                for enabled in (True, False):
                    resp = await client.post(
                        f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": enabled}
                    )
                    assert resp.status == 409
                    assert (await resp.json())["code"] == "structured_monitor_not_convertible"
        assert svc.updates == [] and svc.added == []

    @pytest.mark.asyncio
    async def test_thread_not_open_is_409_and_slot_key_never_comes_from_the_body(
        self, quiet_authz_audit: list[dict[str, Any]]
    ) -> None:
        svc = FakeLoopSvc()
        # A body-supplied slot key is ignored; only the binding counts.
        with _route_patches(svc, binding_slot=None):
            async with TestClient(TestServer(_make_app({"chat-1-1": _slot("")}))) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual",
                    json={"member": CREW, "enabled": True, "slot_key": "chat-1-1"},
                )
        assert resp.status == 409
        assert (await resp.json())["code"] == "member_thread_not_open"
        assert svc.added == []

    @pytest.mark.asyncio
    async def test_bound_slot_that_is_not_member_mode_is_refused(
        self, quiet_authz_audit: list[dict[str, Any]]
    ) -> None:
        svc = FakeLoopSvc()
        with _route_patches(svc, binding_slot="member-scout"):
            async with TestClient(TestServer(_make_app({"member-scout": _slot("")}))) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 409
        assert svc.added == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"member": CREW}, "not_a_boolean"),
            ({"member": CREW, "enabled": "true"}, "not_a_boolean"),
            ({"enabled": True}, "missing_member"),
            ({"member": "other-crew", "enabled": True}, "member_slug_mismatch"),
        ],
    )
    async def test_bad_bodies_are_coded_400s(
        self, quiet_authz_audit: list[dict[str, Any]], body: dict[str, Any], code: str
    ) -> None:
        svc = FakeLoopSvc()
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(f"/api/members/{CREW}/perpetual", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == code
        assert svc.added == [] and svc.updates == []

    @pytest.mark.asyncio
    async def test_service_disabled_is_503(self) -> None:
        with _route_patches(None):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 503
        assert (await resp.json())["code"] == "autonudge_disabled"


# ── (e) the member's own directives on an owner-armed loop ──────────────────


class TestMemberDirectivesOnPerpetualLoop:
    """Inside its wakes the member keeps its interval; caps and the owner's OFF
    are not its to touch, and its own rare stop leaves a readable record."""

    @staticmethod
    def _loop(**overrides: Any) -> NudgeLoop:
        base: dict[str, Any] = dict(
            id="own00020",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            cycle_count=5,
            active=True,
            created_ts=1_000.0,
            next_due_ts=9_000.0,
        )
        base.update(overrides)
        return NudgeLoop(**base)

    @staticmethod
    def _state(loop: NudgeLoop, svc: Any) -> Any:
        return SimpleNamespace(
            _slots={
                loop.slot_key: SimpleNamespace(mode="member", memory_mode="persistent", _app="")
            }
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("patch_body", [{"max_cycles": 24}, {"max_runtime_secs": 3600}])
    async def test_member_cannot_put_a_cap_on_the_owners_perpetual_loop(
        self, trust_home: Path, patch_body: dict[str, Any]
    ) -> None:
        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch.object(sda, "_audit"),
        ):
            with pytest.raises(sda._DirectiveDenied, match="Perpetual mode"):
                await sda._monitor_update(
                    self._state(loop, svc),
                    "dashboard:member-scout",
                    {"patch": patch_body},
                    self_arm_ok=True,
                )
        assert svc.updates == []
        assert loop.max_cycles == 0 and loop.max_runtime_secs == 0

    @pytest.mark.asyncio
    async def test_member_still_retunes_its_own_interval(
        self, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import session_directive_apply as sda

        monkeypatch.setattr(
            autonudge_authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None),
        )
        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)

        async def _update(loop_id: str, **kw: Any) -> Any:
            svc.updates.append({"loop_id": loop_id, **kw})
            if kw.get("idle_secs") is not None:
                loop.idle_secs = int(kw["idle_secs"])
            return loop

        svc.update = _update  # type: ignore[method-assign]
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch.object(sda, "_audit"),
        ):
            result = await sda._monitor_update(
                self._state(loop, svc),
                "dashboard:member-scout",
                {"patch": {"idle_secs": 900}},
                self_arm_ok=True,
            )
        assert "updated" in result.lower()
        assert loop.idle_secs == 900
        assert svc.updates[0]["max_cycles"] is None and svc.updates[0]["active"] is None

    @pytest.mark.asyncio
    async def test_a_cap_on_a_self_armed_member_loop_is_still_the_members_call(
        self, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal is scoped to OWNER-armed loops: a loop the member armed
        itself keeps today's rules (it may cap or uncap its own loop)."""
        from kiro_crew.dashboard import session_directive_apply as sda

        monkeypatch.setattr(
            autonudge_authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None),
        )
        loop = self._loop(self_armed=True)
        sa.record_self_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch.object(sda, "_audit"),
        ):
            await sda._monitor_update(
                self._state(loop, svc),
                "dashboard:member-scout",
                {"patch": {"max_cycles": 24}},
                self_arm_ok=True,
            )
        assert svc.updates and svc.updates[0]["max_cycles"] == 24

    @pytest.mark.asyncio
    async def test_member_cannot_resume_the_owners_off(self, trust_home: Path) -> None:
        """The owner's OFF is a manual pause; monitor_update never resumes one."""
        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop(active=False, stopped_reason="manual", next_due_ts=0.0)
        sa.record_owner_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch.object(sda, "_audit"),
        ):
            with pytest.raises(sda._DirectiveDenied, match="paused manually"):
                await sda._monitor_update(
                    self._state(loop, svc),
                    "dashboard:member-scout",
                    {"patch": {"idle_secs": 60}},
                    self_arm_ok=True,
                )
        assert loop.active is False and svc.updates == []

    @pytest.mark.asyncio
    async def test_members_own_stop_keeps_the_record_with_its_reason(
        self, trust_home: Path
    ) -> None:
        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        slot = SimpleNamespace(mode="member", _app="")
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            result = await sda._autonudge_stop(slot, "dashboard:member-scout", {"reason": "done"})
        assert "stopped on this session" in result
        assert removed == []
        assert loop.active is False
        assert loop.stopped_reason == "autonudge_stop"
        assert loop.next_due_ts == 0.0
        # The owner's ON resumes it later; the member's own re-arm cannot
        # displace it (autonudge_stop is retained evidence in the store).
        from kiro_crew.autonudge import _stopped_row_is_replaceable

        assert _stopped_row_is_replaceable(loop) is False

    @pytest.mark.asyncio
    async def test_a_plain_member_loops_stop_still_removes_it(self, trust_home: Path) -> None:
        """Only the owner-armed loop gains the retained record; a self-armed
        member loop keeps the legacy remove-on-stop behaviour."""
        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop(self_armed=True)
        sa.record_self_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        slot = SimpleNamespace(mode="member", _app="")
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            await sda._autonudge_stop(slot, "dashboard:member-scout", {})
        assert removed == [loop.id]


# ── (f) ownership checks, takeover, fail-closed reads, stop detail ──────────


class TestPerpetualRouteOwnership:
    @pytest.mark.asyncio
    async def test_binding_naming_another_crew_is_refused_and_no_loop_is_touched(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        stopped = NudgeLoop(
            id="old00002", slot_key="member-scout", message="m", idle_secs=60, active=False
        )
        svc = FakeLoopSvc(stopped)
        with _route_patches(svc, binding_member="scout-two"):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 409
        assert (await resp.json())["code"] == "member_pin_mismatch"
        assert svc.updates == [] and svc.added == []
        assert stopped.active is False
        assert not sa.self_arm_record_path().exists()

    @pytest.mark.asyncio
    async def test_stale_generation_binding_is_refused(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """The binding's slot key must be the CURRENT derivation for this
        member (same helper the thread route uses); an older generation's key
        is a thread this switch must not arm."""
        svc = FakeLoopSvc()
        with _route_patches(svc, derived_slot="member-scout.memory-gen2"):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 409
        assert (await resp.json())["code"] == "member_thread_not_open"
        assert svc.added == []

    @pytest.mark.asyncio
    async def test_live_slot_pinned_to_another_crew_is_refused(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        svc = FakeLoopSvc()
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj(agent="other")}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 409
        assert (await resp.json())["code"] == "member_slot_conflict"
        assert svc.added == []


class TestOwnerTakeover:
    @staticmethod
    def _self_armed_stopped() -> NudgeLoop:
        return NudgeLoop(
            id="slf00030",
            slot_key="member-scout",
            message="patrol",
            idle_secs=900,
            max_cycles=24,
            cycle_count=9,
            active=False,
            stopped_reason="manual",
            self_armed=True,
            created_ts=1_000.0,
        )

    @pytest.mark.asyncio
    async def test_takeover_rewrites_the_entry_and_the_loop_follows_owner_rules(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._self_armed_stopped()
        sa.record_self_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["loop"]["active"] is True and body["loop"]["cycle_count"] == 9
        assert body["loop"]["max_cycles"] == 0 and body["loop"]["max_runtime_secs"] == 0
        # One entry per loop, now the owner's; the stored self_armed bit is inert.
        assert sa.recorded_arm_party(loop.id, loop.slot_key) == "owner"
        assert sa.is_recorded_self_arm(loop.id, loop.slot_key) is False
        assert loop.self_armed is True
        # Fire guard: admitted on the owner entry, member slot only.
        from kiro_crew.slack import gateway as gw

        assert await gw.GatewayOrchestrator._dashboard_mode_admits(
            loop, SimpleNamespace(mode="member")
        )
        assert not await gw.GatewayOrchestrator._dashboard_mode_admits(
            loop, SimpleNamespace(mode="crew")
        )
        # Applier: caps refused to the member ...
        state = SimpleNamespace(_slots={"member-scout": _member_slot_obj()})
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch.object(sda, "_audit"),
        ):
            with pytest.raises(sda._DirectiveDenied, match="owner's Perpetual mode"):
                await sda._monitor_update(
                    state, "dashboard:member-scout", {"patch": {"max_cycles": 12}}, self_arm_ok=True
                )
        # ... and its own stop is retained with its words.
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            await sda._autonudge_stop(
                _member_slot_obj(), "dashboard:member-scout", {"reason": "queue drained"}
            )
        assert removed == []
        assert loop.active is False and loop.stopped_reason == "autonudge_stop"
        assert loop.stopped_detail == "queue drained"

    @pytest.mark.asyncio
    async def test_failed_takeover_restores_the_self_entry_and_leaves_the_loop(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        loop = self._self_armed_stopped()
        sa.record_self_arm(loop.id, loop.slot_key)

        class RefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                self.updates.append({"loop_id": loop_id, **kw})
                return None  # the authorizer maps this to "loop not found", 404

        svc = RefusingSvc(loop)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 404
        assert (await resp.json())["code"] == "perpetual_on_failed"
        assert loop.active is False and loop.max_cycles == 24
        assert sa.recorded_arm_party(loop.id, loop.slot_key) == "self"

    @pytest.mark.asyncio
    async def test_failed_takeover_of_an_unrecorded_loop_forgets_the_entry(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        loop = self._self_armed_stopped()
        loop.self_armed = False

        class RefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                return None

        svc = RefusingSvc(loop)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 404
        assert sa.recorded_arm_party(loop.id, loop.slot_key) == ""

    @pytest.mark.asyncio
    async def test_owner_record_write_failure_leaves_the_stopped_loop_alone(
        self, quiet_authz_audit: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = self._self_armed_stopped()

        def _boom(_i: str, _s: str, *, txn: str = "") -> None:
            raise OSError("trust root unavailable")

        monkeypatch.setattr("kiro_crew.autonudge_selfarm.record_owner_arm", _boom)
        monkeypatch.setattr(
            "kiro_crew.autonudge_selfarm.read_arm_party_strict", lambda _i, _s: "self"
        )
        svc = FakeLoopSvc(loop)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
        assert resp.status == 503
        assert svc.updates == [] and loop.active is False


class TestIndeterminateTrustRead:
    """A member slot whose trust record cannot be read fails CLOSED."""

    @staticmethod
    def _loop() -> NudgeLoop:
        return NudgeLoop(
            id="unk00040",
            slot_key="member-scout",
            message="m",
            idle_secs=600,
            max_cycles=0,
            cycle_count=1,
            active=True,
            created_ts=1_000.0,
            next_due_ts=9_000.0,
        )

    @staticmethod
    def _corrupt_record(trust_home: Path) -> None:
        path = sa.self_arm_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

    def test_strict_read_raises_where_the_total_read_answers_none(self, trust_home: Path) -> None:
        self._corrupt_record(trust_home)
        assert sa.recorded_arm_party("unk00040", "member-scout") == ""
        with pytest.raises(OSError):
            sa.read_arm_party_strict("unk00040", "member-scout")
        # A MISSING file is still plain "none" for both.
        sa.self_arm_record_path().unlink()
        assert sa.read_arm_party_strict("unk00040", "member-scout") == ""

    @pytest.mark.asyncio
    async def test_cap_change_is_refused_when_the_record_is_unreadable(
        self, trust_home: Path
    ) -> None:
        from kiro_crew.dashboard import session_directive_apply as sda

        self._corrupt_record(trust_home)
        loop = self._loop()
        svc = FakeLoopSvc(loop)
        state = SimpleNamespace(_slots={"member-scout": _member_slot_obj()})
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch.object(sda, "_audit"),
        ):
            with pytest.raises(sda._DirectiveDenied, match="could not read who armed"):
                await sda._monitor_update(
                    state, "dashboard:member-scout", {"patch": {"max_cycles": 24}}, self_arm_ok=True
                )
        assert svc.updates == []

    @pytest.mark.asyncio
    async def test_stop_retains_the_record_when_the_record_is_unreadable(
        self, trust_home: Path
    ) -> None:
        from kiro_crew.dashboard import session_directive_apply as sda

        self._corrupt_record(trust_home)
        loop = self._loop()
        svc = FakeLoopSvc(loop)
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            await sda._autonudge_stop(_member_slot_obj(), "dashboard:member-scout", {})
        assert removed == []
        assert loop.active is False and loop.stopped_reason == "autonudge_stop"

    @pytest.mark.asyncio
    async def test_non_member_slot_is_unaffected_by_an_unreadable_record(
        self, trust_home: Path
    ) -> None:
        from kiro_crew.dashboard import session_directive_apply as sda

        self._corrupt_record(trust_home)
        loop = NudgeLoop(id="chat0001", slot_key="chat-1-1", message="m", idle_secs=60)
        svc = FakeLoopSvc(loop)
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        plain = SimpleNamespace(mode="", _app="")
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            await sda._autonudge_stop(plain, "dashboard:chat-1-1", {})
        assert removed == ["chat0001"]


class TestStoppedDetailField:
    def test_defaults_and_serialize_load_round_trip(self, tmp_path: Path) -> None:
        import json

        from kiro_crew.autonudge import AutoNudgeService

        loop = NudgeLoop(
            id="det00050",
            slot_key="member-scout",
            message="m",
            idle_secs=60,
            active=False,
            stopped_reason="autonudge_stop",
            stopped_detail="queue drained",
        )
        assert NudgeLoop(id="x", slot_key="s", message="m").stopped_detail == ""
        payload = AutoNudgeService._serialize_loop(loop)
        assert payload["stopped_detail"] == "queue drained"
        # A store written by an older build has no such key: loads as "".
        older = {k: v for k, v in payload.items() if k != "stopped_detail"}
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [payload, {**older, "id": "old00051"}]}),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()
        assert svc._loops["det00050"].stopped_detail == "queue drained"
        assert svc._loops["old00051"].stopped_detail == ""

    def test_non_string_detail_loads_as_empty(self, tmp_path: Path) -> None:
        import json

        from kiro_crew.autonudge import AutoNudgeService

        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "bad00052",
                            "slot_key": "chat-1-1",
                            "message": "m",
                            "idle_secs": 60,
                            "stopped_detail": ["not", "text"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()
        assert svc._loops["bad00052"].stopped_detail == ""

    @pytest.mark.asyncio
    async def test_update_records_detail_on_stop_and_clears_it_on_revival(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.autonudge import AutoNudgeService

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            loop = await svc.add(
                "chat-1-1", "goal", idle_secs=3600, max_cycles=0, stop_sentinel_path=""
            )
            stopped = await svc.update(
                loop.id, active=False, stopped_reason="autonudge_stop", stopped_detail="done"
            )
            assert stopped is not None
            assert stopped.stopped_reason == "autonudge_stop" and stopped.stopped_detail == "done"
            revived = await svc.update(loop.id, active=True)
            assert revived is not None
            assert revived.stopped_reason == "" and revived.stopped_detail == ""
            # A later stop without words does not inherit the earlier text.
            again = await svc.update(loop.id, active=False)
            assert again is not None and again.stopped_detail == ""
        finally:
            svc.stop()


# ── (g) takeover transaction, strict record, output boundary ────────────────


def _takeover_fixture_loop() -> NudgeLoop:
    return NudgeLoop(
        id="slf00060",
        slot_key="member-scout",
        message="patrol",
        idle_secs=900,
        max_cycles=24,
        cycle_count=3,
        active=False,
        stopped_reason="manual",
        self_armed=True,
        created_ts=1_000.0,
    )


class TestTakeoverTransaction:
    @pytest.mark.asyncio
    async def test_update_raising_restores_the_self_entry_and_propagates(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)

        class RaisingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                raise RuntimeError("store wedged")

        svc = RaisingSvc(loop)
        with pytest.raises(RuntimeError, match="store wedged"):
            await _takeover_stopped_loop(svc, loop, "member-scout", caller="t")
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"
        assert loop.active is False and loop.max_cycles == 24

    @pytest.mark.asyncio
    async def test_task_cancellation_leaves_the_entry_as_written(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """A cancellation of the takeover TASK itself (gateway shutdown) is the
        one case whose outcome is unknown -- the service shields its persist --
        so nothing is restored: a stopped loop under an owner entry is
        resumable only by the owner's switch, the safe side. Request-level
        cancellation never reaches here (the handler shields the task)."""
        import asyncio

        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()

        class HangingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                entered.set()
                await asyncio.sleep(3600)
                return self.loop

        svc = HangingSvc(loop)
        task = asyncio.create_task(_takeover_stopped_loop(svc, loop, "member-scout", caller="t"))
        await entered.wait()
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"
        assert loop.active is False

    @pytest.mark.asyncio
    async def test_restore_leaves_an_entry_that_no_longer_says_owner(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """Only this takeover's own change is undone: an entry a later writer
        replaced is theirs."""
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)

        class RewritingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                # Somebody else re-recorded a self arm while we were resuming.
                sa.record_self_arm(loop_id, "member-scout")
                return None

        svc = RewritingSvc(loop)
        _loop, error, status = await _takeover_stopped_loop(svc, loop, "member-scout", caller="t")
        assert error is not None and status == 404
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"

    @pytest.mark.asyncio
    async def test_restore_skipped_when_the_resume_landed_anyway(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """The service shields its persist, so a cancelled update can still
        commit; a cancelled TASK leaves the entry alone, so the loop that did
        resume keeps the owner entry it runs under."""
        import asyncio

        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()

        class CommitThenHangSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                assert self.loop is not None
                self.loop.active = True  # the mutation landed
                entered.set()
                await asyncio.sleep(3600)
                return self.loop

        svc = CommitThenHangSvc(loop)
        task = asyncio.create_task(_takeover_stopped_loop(svc, loop, "member-scout", caller="t"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"

    @pytest.mark.asyncio
    async def test_prior_owner_entry_is_neither_rewritten_nor_restored(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        loop.self_armed = False
        sa.record_owner_arm(loop.id, loop.slot_key)
        writes: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.autonudge_selfarm.record_owner_arm",
            lambda i, s: writes.append(i),
        )

        class RefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                return None

        _loop, error, status = await _takeover_stopped_loop(
            RefusingSvc(loop), loop, "member-scout", caller="t"
        )
        assert status == 404 and error is not None
        assert writes == []
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"

    @pytest.mark.asyncio
    async def test_indeterminate_prior_party_refuses_the_takeover(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        path = sa.self_arm_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        svc = FakeLoopSvc(loop)
        _loop, error, status = await _takeover_stopped_loop(svc, loop, "member-scout", caller="t")
        assert status == 503 and error is not None and "unreadable" in error
        assert svc.updates == [] and loop.active is False
        assert path.read_text(encoding="utf-8") == "{not json"

    @pytest.mark.asyncio
    async def test_concurrent_presses_serialize_and_resume_once(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        import asyncio

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        release = asyncio.Event()
        entered = asyncio.Event()

        class GatedSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                entered.set()
                await release.wait()
                return await super().update(loop_id, **kw)

        svc = GatedSvc(loop)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                body = {"member": CREW, "enabled": True}
                first = asyncio.create_task(
                    client.post(f"/api/members/{CREW}/perpetual", json=body)
                )
                await entered.wait()
                second = asyncio.create_task(
                    client.post(f"/api/members/{CREW}/perpetual", json=body)
                )
                await asyncio.sleep(0.05)
                assert not second.done()
                release.set()
                r1, r2 = await asyncio.gather(first, second)
                assert r1.status == 200 and r2.status == 200
                b2 = await r2.json()
        # One resume, one owner entry; the second press found the loop active.
        assert len(svc.updates) == 1
        assert b2["loop"]["active"] is True and b2["loop"]["max_cycles"] == 0
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"


class TestStrictRecord:
    def test_present_but_malformed_entry_raises(self, trust_home: Path) -> None:
        import json

        sa.record_self_arm("good0001", "member-a")
        path = sa.self_arm_record_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["loops"]["str00001"] = "not a dict"
        data["loops"]["who00001"] = {"slot_key": "member-a", "armed_by": "someone"}
        data["loops"]["nos00001"] = {"armed_by": "self"}
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(OSError):
            sa.read_arm_party_strict("str00001", "member-a")
        with pytest.raises(OSError):
            sa.read_arm_party_strict("who00001", "member-a")
        with pytest.raises(OSError):
            sa.read_arm_party_strict("nos00001", "member-a")
        # Certain answers stay certain.
        assert sa.read_arm_party_strict("good0001", "member-a") == "self"
        assert sa.read_arm_party_strict("good0001", "member-b") == ""
        assert sa.read_arm_party_strict("absent01", "member-a") == ""
        # The total readers keep refusing quietly.
        assert sa.recorded_arm_party("who00001", "member-a") == ""

    def test_write_preserves_malformed_siblings_verbatim(self, trust_home: Path) -> None:
        import json

        sa.record_self_arm("sibl0001", "member-a")
        path = sa.self_arm_record_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["loops"]["odd00001"] = "kept as is"
        data["loops"]["odd00002"] = {"slot_key": 7, "armed_by": "self"}
        path.write_text(json.dumps(data), encoding="utf-8")
        sa.record_owner_arm("newo0001", "member-b")
        sa.forget_self_arm("sibl0001")
        after = json.loads(path.read_text(encoding="utf-8"))["loops"]
        assert after["odd00001"] == "kept as is"
        assert after["odd00002"] == {"slot_key": 7, "armed_by": "self"}
        assert after["newo0001"]["armed_by"] == "owner"
        assert "sibl0001" not in after

    def test_corrupt_file_refuses_the_write_and_is_left_intact(self, trust_home: Path) -> None:
        path = sa.self_arm_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for corrupt in ("{not json", json_dumps({"version": 1, "loops": ["not", "a", "map"]})):
            path.write_text(corrupt, encoding="utf-8")
            with pytest.raises(OSError):
                sa.record_owner_arm("x0000001", "member-a")
            with pytest.raises(OSError):
                sa.record_self_arm("x0000002", "member-a")
            assert path.read_text(encoding="utf-8") == corrupt
            # Revocation is best-effort and must not clobber either.
            sa.forget_self_arm("x0000001")
            assert path.read_text(encoding="utf-8") == corrupt


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj)


class TestStoppedDetailBoundaries:
    POLLUTED = (
        "stopping: key AKIAIOSFODNN7EXAMPLE and ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij "
        + "x" * 900
    )

    def test_load_redacts_and_caps_a_hand_edited_value(self, tmp_path: Path) -> None:
        import json

        from kiro_crew.autonudge import AutoNudgeService

        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "pol00070",
                            "slot_key": "member-scout",
                            "message": "m",
                            "idle_secs": 60,
                            "active": False,
                            "stopped_reason": "autonudge_stop",
                            "stopped_detail": self.POLLUTED,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()
        detail = svc._loops["pol00070"].stopped_detail
        assert len(detail) <= 500
        assert "AKIAIOSFODNN7EXAMPLE" not in detail and "ghp_" not in detail
        assert "[REDACTED" in detail

    def test_serialize_renormalizes_a_raw_in_memory_value(self) -> None:
        from kiro_crew.dashboard.handlers.autonudge import _serialize

        loop = NudgeLoop(
            id="pol00071", slot_key="member-scout", message="m", idle_secs=60, active=False
        )
        loop.stopped_detail = self.POLLUTED  # bypasses update(): the raw store shape
        out = _serialize(loop)["stopped_detail"]
        assert len(out) <= 500
        assert "AKIAIOSFODNN7EXAMPLE" not in out and "ghp_" not in out

    @pytest.mark.asyncio
    async def test_update_write_path_caps_too(self, tmp_path: Path) -> None:
        from kiro_crew.autonudge import AutoNudgeService

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            loop = await svc.add("chat-1-1", "goal", idle_secs=3600, max_cycles=0)
            stopped = await svc.update(
                loop.id, active=False, stopped_reason="autonudge_stop", stopped_detail=self.POLLUTED
            )
            assert stopped is not None
            assert len(stopped.stopped_detail) <= 500
            assert "AKIAIOSFODNN7EXAMPLE" not in stopped.stopped_detail
        finally:
            svc.stop()


# ── (h) supervised takeover, tokens, lock-scope identity ────────────────────


def _mocked_perpetual_request(app: web.Application, body: dict[str, Any]) -> web.Request:
    """A real ``web.Request`` for the handler, with a JSON body, outside TestClient
    -- so the awaiting coroutine can be cancelled the way aiohttp cancels a
    handler whose client went away."""
    import asyncio
    import json
    from unittest.mock import Mock

    from aiohttp import streams
    from aiohttp.test_utils import make_mocked_request

    raw = json.dumps(body).encode("utf-8")
    protocol = Mock(_reading_paused=False)
    payload = streams.StreamReader(protocol, 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(raw)
    payload.feed_eof()
    req = make_mocked_request(
        "POST",
        f"/api/members/{CREW}/perpetual",
        headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
        match_info={"slug": CREW},
        app=app,
        payload=payload,
    )
    req["app"] = ""
    return req


async def _drain_perpetual_tasks(app: web.Application) -> None:
    import asyncio

    from kiro_crew.dashboard.handlers import members as members_handlers

    pending = list(members_handlers._perpetual_tasks_of(app))
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _slow_owner_write(entered: Any, release: Any, aio: Any) -> Any:
    """``record_owner_arm`` that parks in its worker thread until *release* is set."""
    import threading

    real_record = sa.record_owner_arm
    assert isinstance(release, threading.Event)

    def _slow(loop_id: str, slot_key: str, *, txn: str = "") -> None:
        aio.call_soon_threadsafe(entered.set)
        release.wait(timeout=10)
        real_record(loop_id, slot_key, txn=txn)

    return _slow


class TestSupervisedTakeover:
    @pytest.mark.asyncio
    async def test_request_cancelled_during_owner_write_still_completes_the_takeover(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio
        import threading

        from kiro_crew.dashboard.handlers.members import api_member_perpetual_set

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()
        release = threading.Event()
        monkeypatch.setattr(
            "kiro_crew.autonudge_selfarm.record_owner_arm",
            _slow_owner_write(entered, release, asyncio.get_running_loop()),
        )
        svc = FakeLoopSvc(loop)
        app = _make_app({"member-scout": _member_slot_obj()})
        with _route_patches(svc):
            req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
            request_task = asyncio.create_task(api_member_perpetual_set(req))
            await entered.wait()
            request_task.cancel()  # the client went away mid-write
            with pytest.raises(asyncio.CancelledError):
                await request_task
            release.set()
            await _drain_perpetual_tasks(app)
        # No half state: the write landed AND the resume followed it.
        assert loop.active is True and loop.max_cycles == 0
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"
        assert len(svc.updates) == 1

    @pytest.mark.asyncio
    async def test_request_cancelled_during_owner_write_with_refused_resume_restores(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio
        import threading

        from kiro_crew.dashboard.handlers.members import api_member_perpetual_set

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()
        release = threading.Event()
        monkeypatch.setattr(
            "kiro_crew.autonudge_selfarm.record_owner_arm",
            _slow_owner_write(entered, release, asyncio.get_running_loop()),
        )

        class RefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                return None

        svc = RefusingSvc(loop)
        app = _make_app({"member-scout": _member_slot_obj()})
        with _route_patches(svc):
            req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
            request_task = asyncio.create_task(api_member_perpetual_set(req))
            await entered.wait()
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            release.set()
            await _drain_perpetual_tasks(app)
        # The sequence ran to its end and decided on the refusal: entry back to self.
        assert loop.active is False
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"

    @pytest.mark.asyncio
    async def test_request_cancelled_during_update_that_completes_later_keeps_owner(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        import asyncio

        from kiro_crew.dashboard.handlers.members import api_member_perpetual_set

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()
        release = asyncio.Event()

        class LateSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                entered.set()
                await release.wait()  # the service's own lock / persist, finishing later
                return await super().update(loop_id, **kw)

        svc = LateSvc(loop)
        app = _make_app({"member-scout": _member_slot_obj()})
        with _route_patches(svc):
            req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
            request_task = asyncio.create_task(api_member_perpetual_set(req))
            await entered.wait()
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            # The update has not finished; nothing has been restored meanwhile.
            assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"
            release.set()
            await _drain_perpetual_tasks(app)
        assert loop.active is True and loop.max_cycles == 0
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"

    @pytest.mark.asyncio
    async def test_lock_is_released_only_after_the_task_and_its_rollback(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        import asyncio

        from kiro_crew.dashboard.handlers import members as mh

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()
        release = asyncio.Event()

        class LateRefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                entered.set()
                await release.wait()
                return None

        svc = LateRefusingSvc(loop)
        app = _make_app({"member-scout": _member_slot_obj()})
        with _route_patches(svc):
            req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
            request_task = asyncio.create_task(mh.api_member_perpetual_set(req))
            await entered.wait()
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            # The slot lock is still held by the supervised task.
            assert mh._PERPETUAL_LOCKS["member-scout"].locked()
            release.set()
            await _drain_perpetual_tasks(app)
        # Released, rolled back, and the per-slot entry dropped once idle.
        assert "member-scout" not in mh._PERPETUAL_LOCKS
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"
        assert loop.active is False


class TestTakeoverToken:
    def test_owner_entry_carries_the_token_and_the_strict_reader_returns_it(
        self, trust_home: Path
    ) -> None:
        sa.record_owner_arm("tok00001", "member-a", txn="abc")
        assert sa.read_arm_entry_strict("tok00001", "member-a") == ("owner", "abc")
        assert sa.read_arm_party_strict("tok00001", "member-a") == "owner"
        # Arm-time entries carry none; self entries carry none.
        sa.record_owner_arm("tok00002", "member-a")
        sa.record_self_arm("tok00003", "member-a")
        assert sa.read_arm_entry_strict("tok00002", "member-a") == ("owner", "")
        assert sa.read_arm_entry_strict("tok00003", "member-a") == ("self", "")
        assert sa.read_arm_entry_strict("tok00001", "member-b") == ("", "")

    def test_restore_touches_only_its_own_token(self, trust_home: Path) -> None:
        from kiro_crew.dashboard.handlers.members import _restore_arm_party

        # Takeover A wrote token "aaa"; takeover B then wrote "bbb".
        sa.record_owner_arm("tok00010", "member-a", txn="aaa")
        sa.record_owner_arm("tok00010", "member-a", txn="bbb")
        _restore_arm_party("tok00010", "member-a", "self", "aaa")
        assert sa.read_arm_entry_strict("tok00010", "member-a") == ("owner", "bbb")
        # B's own late rollback still works.
        _restore_arm_party("tok00010", "member-a", "self", "bbb")
        assert sa.read_arm_entry_strict("tok00010", "member-a") == ("self", "")
        # No token -> nothing was minted -> nothing to restore.
        sa.record_owner_arm("tok00011", "member-a")
        _restore_arm_party("tok00011", "member-a", "", "")
        assert sa.read_arm_party_strict("tok00011", "member-a") == "owner"

    @pytest.mark.asyncio
    async def test_late_rollback_does_not_clobber_the_next_takeover(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """Takeover A's resume is refused; while A was mid-update, takeover B
        (a later press) already rewrote the entry with ITS token. A's rollback
        must leave B's entry alone."""
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)

        class RefusedWhileSupersededSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                sa.record_owner_arm(loop_id, "member-scout", txn="next-takeover")
                return None

        svc = RefusedWhileSupersededSvc(loop)
        _loop, error, status = await _takeover_stopped_loop(svc, loop, "member-scout", caller="t")
        assert error is not None and status == 404
        assert sa.read_arm_entry_strict(loop.id, loop.slot_key) == ("owner", "next-takeover")


class TestLockScopeIdentity:
    @pytest.mark.asyncio
    async def test_binding_changed_while_waiting_for_the_lock_is_refused(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        import asyncio
        import json

        from kiro_crew.dashboard.handlers import members as mh

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        bindings = iter(
            [
                {"member": CREW, "slot_key": "member-scout"},  # the pre-lock check
                {"member": "someone-else", "slot_key": "member-scout"},  # under the lock
            ]
        )
        with _route_patches(svc):
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.read_dm_binding",
                side_effect=lambda _slug: next(bindings),
            ):
                app = _make_app({"member-scout": _member_slot_obj()})
                holder = mh._perpetual_lock("member-scout")
                await holder.__aenter__()  # somebody else holds the slot lock
                try:
                    req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
                    request_task = asyncio.create_task(mh.api_member_perpetual_set(req))
                    await asyncio.sleep(0.05)
                    assert not request_task.done()
                finally:
                    await holder.__aexit__(None, None, None)
                resp = await request_task
        assert resp.status == 409
        assert json.loads(resp.text or "")["code"] == "member_pin_mismatch"
        assert svc.updates == [] and loop.active is False
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"

    @pytest.mark.asyncio
    async def test_slot_key_changed_while_waiting_for_the_lock_is_refused(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        import asyncio
        import json

        from kiro_crew.dashboard.handlers import members as mh

        loop = _takeover_fixture_loop()
        svc = FakeLoopSvc(loop)
        slots = {
            "member-scout": _member_slot_obj(),
            "member-scout.memory-gen2": _member_slot_obj(),
        }
        derived = iter(["member-scout", "member-scout.memory-gen2"])
        bindings = iter(
            [
                {"member": CREW, "slot_key": "member-scout"},
                {"member": CREW, "slot_key": "member-scout.memory-gen2"},
            ]
        )
        with _route_patches(svc):
            with (
                patch(
                    "kiro_crew.dashboard.handlers.members.members_mod.read_dm_binding",
                    side_effect=lambda _slug: next(bindings),
                ),
                patch(
                    "kiro_crew.dashboard.handlers.members._member_thread_slot",
                    side_effect=lambda _cfg, _m, _s: (next(derived), ""),
                ),
            ):
                app = _make_app(slots)
                holder = mh._perpetual_lock("member-scout")
                await holder.__aenter__()
                try:
                    req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
                    request_task = asyncio.create_task(mh.api_member_perpetual_set(req))
                    await asyncio.sleep(0.05)
                finally:
                    await holder.__aexit__(None, None, None)
                resp = await request_task
        assert resp.status == 409
        assert json.loads(resp.text or "")["code"] == "member_slot_conflict"
        assert svc.updates == [] and svc.added == []

    @pytest.mark.asyncio
    async def test_lock_map_is_bounded(self) -> None:
        from kiro_crew.dashboard.handlers import members as mh

        async with mh._perpetual_lock("member-x"):
            assert "member-x" in mh._PERPETUAL_LOCKS
            assert mh._PERPETUAL_LOCK_USERS["member-x"] == 1
        assert "member-x" not in mh._PERPETUAL_LOCKS
        assert "member-x" not in mh._PERPETUAL_LOCK_USERS


class TestPersistenceFailureRollback:
    @pytest.mark.asyncio
    async def test_in_memory_flip_then_none_rolls_back(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """The decision reads the RETURNED loop, never ``existing.active``: a
        service whose persist failed returns None after flipping and restoring
        its fields, and the takeover must roll the trust entry back."""
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)

        class FlipThenFailSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                assert self.loop is not None
                self.loop.active = True  # visible mid-write ...
                self.loop.active = False  # ... rolled back by the service
                return None

        _loop, error, status = await _takeover_stopped_loop(
            FlipThenFailSvc(loop), loop, "member-scout", caller="t"
        )
        assert error is not None and status == 404
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"

    @pytest.mark.asyncio
    async def test_in_memory_flip_then_raise_rolls_back(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)

        class FlipThenRaiseSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                assert self.loop is not None
                self.loop.active = True
                raise OSError("disk full")

        with pytest.raises(OSError):
            await _takeover_stopped_loop(FlipThenRaiseSvc(loop), loop, "member-scout", caller="t")
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"

    @pytest.mark.asyncio
    async def test_returned_inactive_loop_counts_as_not_resumed(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)

        class StillInactiveSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                return self.loop  # unchanged, still inactive

        _loop, error, status = await _takeover_stopped_loop(
            StillInactiveSvc(loop), loop, "member-scout", caller="t"
        )
        assert error == "loop did not resume" and status == 409
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"


# ── (i) task lifecycle and the atomic conditional restore ───────────────────


class TestAtomicConditionalRestore:
    def test_token_mismatch_leaves_the_entry_untouched(self, trust_home: Path) -> None:
        sa.record_owner_arm("atm00001", "member-a", txn="bbb")
        before = sa.self_arm_record_path().read_text(encoding="utf-8")
        assert sa.restore_arm_party_if_token("atm00001", "member-a", "aaa", "self") is False
        assert sa.self_arm_record_path().read_text(encoding="utf-8") == before
        assert sa.read_arm_entry_strict("atm00001", "member-a") == ("owner", "bbb")

    def test_match_restores_self_or_removes(self, trust_home: Path) -> None:
        sa.record_owner_arm("atm00002", "member-a", txn="t2")
        assert sa.restore_arm_party_if_token("atm00002", "member-a", "t2", "self") is True
        assert sa.read_arm_entry_strict("atm00002", "member-a") == ("self", "")
        sa.record_owner_arm("atm00003", "member-a", txn="t3")
        assert sa.restore_arm_party_if_token("atm00003", "member-a", "t3", "") is True
        assert sa.read_arm_entry_strict("atm00003", "member-a") == ("", "")
        # Nothing to restore: blank token, owner prior party, another slot, no entry.
        sa.record_owner_arm("atm00004", "member-a", txn="t4")
        assert sa.restore_arm_party_if_token("atm00004", "member-a", "", "self") is False
        assert sa.restore_arm_party_if_token("atm00004", "member-a", "t4", "owner") is False
        assert sa.restore_arm_party_if_token("atm00004", "member-b", "t4", "self") is False
        assert sa.restore_arm_party_if_token("absent99", "member-a", "t4", "self") is False
        assert sa.read_arm_entry_strict("atm00004", "member-a") == ("owner", "t4")

    def test_siblings_survive_the_locked_rewrite_verbatim(self, trust_home: Path) -> None:
        import json

        sa.record_self_arm("sib00001", "member-a")
        sa.record_owner_arm("sib00002", "member-b", txn="other")
        sa.record_owner_arm("tgt00001", "member-c", txn="mine")
        path = sa.self_arm_record_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["loops"]["odd00001"] = "kept as is"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert sa.restore_arm_party_if_token("tgt00001", "member-c", "mine", "") is True
        after = json.loads(path.read_text(encoding="utf-8"))["loops"]
        assert "tgt00001" not in after
        assert after["odd00001"] == "kept as is"
        assert after["sib00001"]["armed_by"] == "self"
        assert after["sib00002"] == {**after["sib00002"], "armed_by": "owner", "txn": "other"}

    def test_malformed_target_or_file_raises_and_leaves_the_file(self, trust_home: Path) -> None:
        import json

        sa.record_owner_arm("bad00001", "member-a", txn="t")
        path = sa.self_arm_record_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["loops"]["bad00001"] = "not a dict"
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        with pytest.raises(OSError):
            sa.restore_arm_party_if_token("bad00001", "member-a", "t", "self")
        assert path.read_text(encoding="utf-8") == before
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(OSError):
            sa.restore_arm_party_if_token("bad00001", "member-a", "t", "self")
        assert path.read_text(encoding="utf-8") == "{not json"
        with pytest.raises(ValueError):
            sa.restore_arm_party_if_token("bad00001", "member-a", "t", "someone")

    def test_handler_wrapper_is_thin_and_never_raises(self, trust_home: Path) -> None:
        from kiro_crew.dashboard.handlers.members import _restore_arm_party

        sa.record_owner_arm("thin0001", "member-a", txn="t")
        assert _restore_arm_party("thin0001", "member-a", "self", "t") is True
        assert sa.read_arm_entry_strict("thin0001", "member-a") == ("self", "")
        path = sa.self_arm_record_path()
        path.write_text("{not json", encoding="utf-8")
        assert _restore_arm_party("thin0001", "member-a", "self", "t") is False


class TestPerpetualTaskLifecycle:
    @pytest.mark.asyncio
    async def test_done_callback_logs_a_failure_by_type_and_slot_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import asyncio
        import functools
        import logging

        from kiro_crew.dashboard.handlers import members as mh

        async def _boom() -> None:
            raise RuntimeError("body text that must not be templated: secret-reason")

        tasks: set[asyncio.Task[Any]] = set()
        task = asyncio.ensure_future(_boom())
        tasks.add(task)
        task.add_done_callback(
            functools.partial(mh._perpetual_task_done, tasks=tasks, slot_key="member-scout")
        )
        with caplog.at_level(logging.DEBUG, logger=mh.logger.name):
            with pytest.raises(RuntimeError):
                await task
            await asyncio.sleep(0)  # let the callback run
        errors = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and "perpetual mutation" in r.getMessage()
        ]
        assert errors, caplog.text
        msg = errors[-1].getMessage()
        assert "member-scout" in msg and "RuntimeError" in msg
        # Type only on the error line: no message text, no chain.
        assert "secret-reason" not in msg
        assert errors[-1].exc_info is None
        # The chain is kept, at debug.
        debugs = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "failure detail" in r.getMessage()
        ]
        assert debugs and debugs[-1].exc_info is not None
        assert task not in tasks

    @pytest.mark.asyncio
    async def test_done_callback_logs_a_cancellation_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import asyncio
        import functools
        import logging

        from kiro_crew.dashboard.handlers import members as mh

        tasks: set[asyncio.Task[Any]] = set()
        task = asyncio.ensure_future(asyncio.sleep(3600))
        tasks.add(task)
        task.add_done_callback(
            functools.partial(mh._perpetual_task_done, tasks=tasks, slot_key="member-scout")
        )
        with caplog.at_level(logging.WARNING, logger=mh.logger.name):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
        warned = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "cancelled" in r.getMessage()
        ]
        assert warned and "member-scout" in warned[-1].getMessage()
        assert "may have left its trust entry saying owner" in warned[-1].getMessage()

    @pytest.mark.asyncio
    async def test_shutdown_refuses_new_mutations_and_drains_outstanding_tasks(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import asyncio
        import functools
        import json
        import logging

        from kiro_crew.dashboard.handlers import members as mh

        monkeypatch.setattr(mh, "_PERPETUAL_DRAIN_GRACE_SECS", 0.05)
        app = _make_app({"member-scout": _member_slot_obj()})
        mh.register_perpetual_lifecycle(app)
        assert mh._perpetual_stop_admitting in app.on_shutdown
        assert mh._perpetual_drain in app.on_cleanup
        # An outstanding mutation that will not finish on its own.
        tasks = mh._perpetual_tasks_of(app)
        hanging = asyncio.ensure_future(asyncio.sleep(3600))
        tasks.add(hanging)
        hanging.add_done_callback(
            functools.partial(mh._perpetual_task_done, tasks=tasks, slot_key="member-scout")
        )
        # on_shutdown: the route stops admitting.
        await mh._perpetual_stop_admitting(app)
        svc = FakeLoopSvc(_takeover_fixture_loop())
        with _route_patches(svc):
            resp = await mh.api_member_perpetual_set(
                _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
            )
        assert resp.status == 503
        assert json.loads(resp.text or "")["code"] == "shutting_down"
        assert svc.updates == [] and svc.added == []
        # on_cleanup: bounded grace, then cancel-and-join.
        with caplog.at_level(logging.WARNING, logger=mh.logger.name):
            await mh._perpetual_drain(app)
        assert hanging.cancelled()
        assert hanging not in tasks
        assert any("still running at shutdown" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_drain_with_nothing_outstanding_is_a_no_op(self) -> None:
        from kiro_crew.dashboard.handlers import members as mh

        app = web.Application()
        mh.register_perpetual_lifecycle(app)
        assert not mh._perpetual_tasks_of(app)
        with patch("kiro_crew.autonudge.get_instance", return_value=None):
            await mh._perpetual_drain(app)

    @pytest.mark.asyncio
    async def test_lifecycle_is_registered_by_the_routes_registrar(self) -> None:
        from kiro_crew.dashboard.handlers import members as mh
        from kiro_crew.dashboard.routes import agents as agents_routes

        app = web.Application()
        agents_routes.register(app)
        assert mh._perpetual_stop_admitting in app.on_shutdown
        assert mh._perpetual_drain in app.on_cleanup


# ── (j) joined thread writes, per-app task sets ─────────────────────────────


def _parked_write(entered: Any, release: Any, aio: Any, real: Any, finished: list[str]) -> Any:
    """A blocking write that parks in its worker thread until *release* is set,
    then does the real write and records that it finished."""

    def _slow(*args: Any, **kwargs: Any) -> Any:
        aio.call_soon_threadsafe(entered.set)
        release.wait(timeout=10)
        out = real(*args, **kwargs)
        finished.append("done")
        return out

    return _slow


class TestJoinedThreadWrites:
    @pytest.mark.asyncio
    async def test_cancel_during_restore_joins_the_thread_before_the_lock_is_released(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio
        import threading

        from kiro_crew.dashboard.handlers import members as mh

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()
        release = threading.Event()
        finished: list[str] = []
        monkeypatch.setattr(
            mh,
            "_restore_arm_party",
            _parked_write(
                entered, release, asyncio.get_running_loop(), mh._restore_arm_party, finished
            ),
        )

        class RefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                return None  # refused -> the takeover rolls back

        svc = RefusingSvc(loop)
        app = _make_app({"member-scout": _member_slot_obj()})
        with _route_patches(svc):
            req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
            request_task = asyncio.create_task(mh.api_member_perpetual_set(req))
            await entered.wait()  # the restore thread is parked, lock held
            (supervised,) = list(mh._perpetual_tasks_of(app))
            supervised.cancel()  # task-level cancel while awaiting the restore
            with pytest.raises(asyncio.CancelledError):
                await request_task
            # Cancelled, but not yet finished: the join is waiting on the thread.
            await asyncio.sleep(0.05)
            assert not supervised.done()
            assert mh._PERPETUAL_LOCKS["member-scout"].locked()
            assert finished == []
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await supervised
        # The thread finished BEFORE the task ended and the lock was released.
        assert finished == ["done"]
        assert "member-scout" not in mh._PERPETUAL_LOCKS
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"

    @pytest.mark.asyncio
    async def test_cancel_during_owner_write_joins_the_thread(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio
        import threading

        from kiro_crew.dashboard.handlers import members as mh

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()
        release = threading.Event()
        finished: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.autonudge_selfarm.record_owner_arm",
            _parked_write(
                entered, release, asyncio.get_running_loop(), sa.record_owner_arm, finished
            ),
        )
        svc = FakeLoopSvc(loop)
        app = _make_app({"member-scout": _member_slot_obj()})
        with _route_patches(svc):
            req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
            request_task = asyncio.create_task(mh.api_member_perpetual_set(req))
            await entered.wait()
            (supervised,) = list(mh._perpetual_tasks_of(app))
            supervised.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            await asyncio.sleep(0.05)
            assert not supervised.done() and finished == []
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await supervised
        assert finished == ["done"]
        # Task-level cancel: the entry is left as written (owner), the loop untouched.
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"
        assert loop.active is False and svc.updates == []
        assert "member-scout" not in mh._PERPETUAL_LOCKS

    @pytest.mark.asyncio
    async def test_drain_covers_a_restore_thread_in_flight(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio
        import threading

        from kiro_crew.dashboard.handlers import members as mh

        monkeypatch.setattr(mh, "_PERPETUAL_DRAIN_GRACE_SECS", 0.2)
        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered = asyncio.Event()
        release = threading.Event()
        finished: list[str] = []
        monkeypatch.setattr(
            mh,
            "_restore_arm_party",
            _parked_write(
                entered, release, asyncio.get_running_loop(), mh._restore_arm_party, finished
            ),
        )

        class RefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                return None

        svc = RefusingSvc(loop)
        app = _make_app({"member-scout": _member_slot_obj()})
        mh.register_perpetual_lifecycle(app)
        with _route_patches(svc):
            req = _mocked_perpetual_request(app, {"member": CREW, "enabled": True})
            request_task = asyncio.create_task(mh.api_member_perpetual_set(req))
            await entered.wait()
            # Let the thread go partway through the drain's grace, then finish.
            asyncio.get_running_loop().call_later(0.1, release.set)
            with patch("kiro_crew.autonudge.get_instance", return_value=None):
                await mh._perpetual_drain(app)
            await asyncio.gather(request_task, return_exceptions=True)
        # When the drain returned, the restore thread had already finished.
        assert finished == ["done"]
        assert not mh._perpetual_tasks_of(app)
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"

    @pytest.mark.asyncio
    async def test_await_thread_to_completion_rejoins_on_cancel(self) -> None:
        import asyncio
        import threading

        from kiro_crew.autonudge_selfarm import await_thread_to_completion

        release = threading.Event()
        finished: list[str] = []

        def _work() -> str:
            release.wait(timeout=10)
            finished.append("done")
            return "ok"

        task = asyncio.create_task(await_thread_to_completion(_work))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()  # joining the thread
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == ["done"]
        # The happy path returns the thread's value.
        assert await await_thread_to_completion(lambda: 42) == 42

    @pytest.mark.asyncio
    async def test_drain_waits_on_the_services_in_flight_writes_without_cancelling(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import asyncio
        import logging

        from kiro_crew.dashboard.handlers import members as mh

        monkeypatch.setattr(mh, "_PERPETUAL_DRAIN_GRACE_SECS", 0.05)
        app = web.Application()
        mh.register_perpetual_lifecycle(app)
        slow = asyncio.ensure_future(asyncio.sleep(3600))
        fake_svc = SimpleNamespace(_inflight_adds={slow})
        with caplog.at_level(logging.INFO, logger=mh.logger.name):
            with patch("kiro_crew.autonudge.get_instance", return_value=fake_svc):
                await mh._perpetual_drain(app)
        # Not cancelled -- the service owns it -- but reported.
        assert not slow.done()
        assert any("still in flight" in r.getMessage() for r in caplog.records)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow


class TestPerAppTaskSets:
    @pytest.mark.asyncio
    async def test_each_app_drains_only_its_own_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from kiro_crew.dashboard.handlers import members as mh

        monkeypatch.setattr(mh, "_PERPETUAL_DRAIN_GRACE_SECS", 0.05)
        app_a = web.Application()
        app_b = web.Application()
        mh.register_perpetual_lifecycle(app_a)
        mh.register_perpetual_lifecycle(app_b)
        assert mh._perpetual_tasks_of(app_a) is not mh._perpetual_tasks_of(app_b)
        task_a = asyncio.ensure_future(asyncio.sleep(3600))
        task_b = asyncio.ensure_future(asyncio.sleep(3600))
        mh._perpetual_tasks_of(app_a).add(task_a)
        mh._perpetual_tasks_of(app_b).add(task_b)
        with patch("kiro_crew.autonudge.get_instance", return_value=None):
            await mh._perpetual_drain(app_a)
        assert task_a.cancelled()
        assert not task_b.done()
        # And app A's shutdown flag does not close app B's route.
        await mh._perpetual_stop_admitting(app_a)
        assert app_a.get(mh._PERPETUAL_SHUTDOWN_KEY) is True
        assert app_b.get(mh._PERPETUAL_SHUTDOWN_KEY) is None
        task_b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_b

    @pytest.mark.asyncio
    async def test_an_unregistered_app_still_gets_a_set_on_first_use(self) -> None:
        from kiro_crew.dashboard.handlers import members as mh

        app = web.Application()
        assert mh._PERPETUAL_TASKS_KEY not in app
        tasks = mh._perpetual_tasks_of(app)
        assert tasks == set() and mh._perpetual_tasks_of(app) is tasks


# ── (k) the authorizer's own owner write, second-cancel rejoin ──────────────


class TestAuthorizerOwnerWriteIsJoined:
    @pytest.mark.asyncio
    async def test_no_loop_owner_arm_joins_its_record_write_on_cancel(
        self,
        audits: list[dict[str, Any]],
        trust_home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 'no existing loop' owner arm writes the trust entry from inside
        ``authorize_and_add_nudge``; a cancel there must not leave that thread
        writing after the arm has unwound."""
        import asyncio
        import threading

        entered = asyncio.Event()
        release = threading.Event()
        finished: list[str] = []
        monkeypatch.setattr(
            autonudge_authz,
            "record_owner_arm",
            _parked_write(
                entered, release, asyncio.get_running_loop(), sa.record_owner_arm, finished
            ),
        )
        svc = RecordingSvc()
        task = asyncio.create_task(
            authorize_and_add_nudge(
                svc=svc,
                state=_state({"member-scout": _slot("member")}),
                slot_key="member-scout",
                message="perpetual",
                stop_sentinel_path=str(tmp_path / "stop"),
                source="dashboard",
                replace_existing=False,
                owner_arm=True,
            )
        )
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done() and finished == []  # joining the parked thread
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == ["done"]
        # The entry landed, the loop did not: a stopped-or-absent loop under an
        # owner entry is the safe side, and the add never ran.
        assert svc.added == []

    @pytest.mark.asyncio
    async def test_second_cancel_during_the_rejoin_still_waits_for_the_thread(self) -> None:
        import asyncio
        import threading

        from kiro_crew.autonudge_selfarm import await_thread_to_completion

        release = threading.Event()
        finished: list[str] = []

        def _work() -> None:
            release.wait(timeout=10)
            finished.append("done")

        task = asyncio.create_task(await_thread_to_completion(_work))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()  # the drain's forced second cancel
        await asyncio.sleep(0.05)
        assert not task.done() and finished == []  # still joining
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == ["done"]

    @pytest.mark.asyncio
    async def test_threads_own_exception_does_not_mask_the_cancel(self) -> None:
        import asyncio
        import threading

        from kiro_crew.autonudge_selfarm import await_thread_to_completion

        release = threading.Event()

        def _work() -> None:
            release.wait(timeout=10)
            raise OSError("disk full")

        task = asyncio.create_task(await_thread_to_completion(_work))
        await asyncio.sleep(0.05)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Uncancelled, the thread's error is the caller's to see.
        with pytest.raises(OSError):
            await await_thread_to_completion(_work)
