"""Perpetual mode: the dashboard OWNER arms a member's own loop from its detail page.

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

import hashlib
import hmac
import json
import logging
import os
import shutil
import stat
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
    # The seal key is the dashboard's token secret; pin it so a test never
    # loads (or creates) the real ``token_signing.key`` and so a rotation can
    # be staged by re-pinning.
    monkeypatch.setattr(sa, "_seal_secret", lambda: b"test-seal-key")
    return tmp_path


def _rewrite_record(mutate: Any) -> Path:
    """Hand-edit the record's ``loops`` map and write it back SEALED.

    The gateway seals every record it writes; a test that edits the file in
    place without re-sealing stages a plant, not an edit, and the readers
    refuse it whole -- see ``TestRecordSeal``. *mutate* receives the loops map.
    """
    path = sa.self_arm_record_path()
    loops = json.loads(path.read_text(encoding="utf-8"))["loops"]
    mutate(loops)
    path.write_text(json.dumps({"version": 2, "loops": loops, "seal": sa._seal(loops)}))
    return path


def _entry(loop_id: str, slot_key: str) -> tuple[str, str]:
    """``(party, txn)`` as stored, through the strict party reader plus the raw
    record for the token -- the token has no reader of its own in ``src/``."""
    party = sa.read_arm_party_strict(loop_id, slot_key)
    if not party:
        return "", ""
    return party, str(sa._read_record_strict_raw()[loop_id].get("txn", ""))


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
        assert '"armed_by": "self"' in sa.self_arm_record_path().read_text(encoding="utf-8")
        # Simulate a record written before the field existed.
        _rewrite_record(lambda loops: loops["old00001"].pop("armed_by"))
        assert "armed_by" not in sa.self_arm_record_path().read_text(encoding="utf-8")
        assert sa.is_recorded_self_arm("old00001", "member-a") is True
        assert sa.is_recorded_owner_arm("old00001", "member-a") is False

    def test_unknown_party_vouches_for_nobody(self, trust_home: Path) -> None:
        sa.record_owner_arm("x0000001", "member-a")
        _rewrite_record(lambda loops: loops["x0000001"].__setitem__("armed_by", "someone"))
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


class TestFireFenceAgainstOff:
    """The fire path on a MEMBER slot holds the perpetual per-slot lock from
    admission through turn publication and re-reads the loop under it. An OFF
    that pauses the loop and revokes the entry while the timer is at the
    admission read must win: no wake is published after the owner said stop."""

    @staticmethod
    def _orchestrator(svc: Any) -> Any:
        from unittest.mock import MagicMock

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.slack import gateway as gw

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U"}):
            orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
        slot = MagicMock()
        slot.key = "member-scout"
        slot.running = False
        slot._in_stage_execution = False
        slot.is_closing = False
        slot.mode = "member"
        slot.memory_mode = "persistent"
        orch.dashboard_state = SimpleNamespace(
            get_slot=MagicMock(return_value=slot),
            push_slots_update=MagicMock(),
            _background_tasks=set(),
            run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
        )
        orch.autonudge_svc = svc
        orch._session_tasks = {}
        return orch, slot

    @staticmethod
    def _loop() -> NudgeLoop:
        return NudgeLoop(
            id="own00030",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            active=True,
            next_due_ts=9_000.0,
        )

    async def _fire(self, orch: Any, loop: NudgeLoop) -> tuple[Any, list[Any]]:
        from unittest.mock import AsyncMock as _AsyncMock

        from kiro_crew.slack import gateway as gw

        spawned: list[Any] = []

        def _spawn(_state: Any, _slot: Any, coro: Any) -> Any:
            import asyncio

            task = asyncio.create_task(coro)
            spawned.append(task)
            return task

        async def _run_chat(*_a: Any, **_kw: Any) -> None:
            return None

        with (
            patch.object(gw, "spawn_guarded_turn", _spawn),
            patch.object(gw, "compose_nudge_body", _AsyncMock(return_value="body")),
            patch.object(
                gw, "sel", return_value=SimpleNamespace(log_tool_invocation=lambda **kw: None)
            ),
            patch("kiro_crew.dashboard.chat._run_chat", new=_run_chat),
        ):
            result = await orch._fire_dashboard_nudge(loop)
            for task in spawned:
                await task
        return result, spawned

    @pytest.mark.asyncio
    async def test_owner_armed_wake_dispatches_when_nothing_races(self, trust_home: Path) -> None:
        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        orch, slot = self._orchestrator(FakeLoopSvc(loop))
        result, spawned = await self._fire(orch, loop)
        # No ``wake_message``: the legacy fire path answers a bool (``_delivery_result``).
        assert result is True
        assert len(spawned) == 1
        slot.append.assert_called_once()
        assert "member-scout" not in sa._PERPETUAL_LOCKS

    @pytest.mark.asyncio
    async def test_off_that_wins_the_lock_leaves_no_wake_behind(self, trust_home: Path) -> None:
        """OFF holds the lock before the timer arrives, pauses + revokes under
        it, releases; the timer, blocked at admission, then re-reads under the
        lock and refuses. Before the fence the admission read (entry present)
        happened first and the wake was published on a loop OFF had paused."""
        import asyncio

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        orch, slot = self._orchestrator(svc)
        held, release = asyncio.Event(), asyncio.Event()

        async def _off() -> None:
            async with sa.perpetual_slot_lock(loop.slot_key):
                held.set()
                await release.wait()
                await svc.update(loop.id, active=False, stopped_reason="manual")
                sa.revoke_arm(loop.id)

        off = asyncio.create_task(_off())
        await held.wait()
        fire = asyncio.create_task(self._fire(orch, loop))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not fire.done(), "the timer must wait for OFF at the perpetual lock"
        release.set()
        await off
        result, spawned = await fire
        assert result is False
        assert spawned == [], "no turn may be published after OFF"
        slot.append.assert_not_called()
        assert loop.active is False
        assert "member-scout" not in sa._PERPETUAL_LOCKS

    @pytest.mark.asyncio
    async def test_a_paused_loop_is_refused_under_the_lock_even_with_a_stale_entry(
        self, trust_home: Path
    ) -> None:
        """The live re-read is its own check: an entry that outlived a pause
        (a refused revoke) still does not buy a wake for a paused loop."""
        loop = self._loop()
        loop.active = False
        loop.stopped_reason = "manual"
        sa.record_owner_arm(loop.id, loop.slot_key)
        orch, slot = self._orchestrator(FakeLoopSvc(loop))
        result, spawned = await self._fire(orch, loop)
        assert result is False
        assert spawned == []
        slot.append.assert_not_called()

    def test_only_a_member_slot_takes_the_lock(self) -> None:
        import contextlib

        from kiro_crew.slack import gateway as gw

        loop = self._loop()
        member = gw.GatewayOrchestrator._member_admission_lock(loop, SimpleNamespace(mode="member"))
        assert isinstance(member, sa.perpetual_slot_lock)
        for mode in ("crew", "dashboard", ""):
            other = gw.GatewayOrchestrator._member_admission_lock(loop, SimpleNamespace(mode=mode))
            assert isinstance(other, contextlib.nullcontext)


class TestFireOccupancyRecheckUnderLock:
    """The pre-lock occupancy check is not the publication decision. While the
    timer waits for the perpetual lock (owner ON holding it), or during the
    admission read under it, the user can send a turn on the same member slot
    -- a chat send publishes ``slot.task`` under no lock. The fire path must
    re-read occupancy under the lock, and once more at publication time, and
    answer BUSY without overwriting that task, appending a row, or registering
    a session task. Reuses ``TestFireFenceAgainstOff``'s harness."""

    _orchestrator = staticmethod(TestFireFenceAgainstOff._orchestrator)
    _loop = staticmethod(TestFireFenceAgainstOff._loop)

    @staticmethod
    async def _fire(orch: Any, loop: NudgeLoop) -> tuple[Any, list[Any]]:
        return await TestFireFenceAgainstOff()._fire(orch, loop)

    @staticmethod
    async def _hold_lock_until(slot_key: str, held: Any, release: Any) -> None:
        async with sa.perpetual_slot_lock(slot_key):
            held.set()
            await release.wait()

    @staticmethod
    def _assert_busy_left_nothing(orch: Any, slot: Any, user_turn: Any) -> None:
        assert slot.task is user_turn, "the user's task is never overwritten"
        assert orch._session_tasks == {}, "a BUSY wake registers no session task"
        slot.append.assert_not_called()
        orch.dashboard_state.push_slots_update.assert_not_called()
        assert "member-scout" not in sa._PERPETUAL_LOCKS

    @pytest.mark.asyncio
    async def test_user_turn_started_during_lock_wait_wins_over_the_wake(
        self, trust_home: Path
    ) -> None:
        import asyncio

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        orch, slot = self._orchestrator(FakeLoopSvc(loop))
        held, release = asyncio.Event(), asyncio.Event()
        owner_on = asyncio.create_task(self._hold_lock_until(loop.slot_key, held, release))
        await held.wait()
        # The fast check sees an idle slot and the timer queues for the lock.
        fire = asyncio.create_task(self._fire(orch, loop))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not fire.done(), "the timer must wait for the owner at the perpetual lock"
        # A user turn starts on the slot while the timer waits: the send path
        # publishes the task synchronously and takes no lock.
        user_turn = object()
        slot.task = user_turn
        slot.running = True
        release.set()
        await owner_on
        result, spawned = await fire
        assert result is False, "the wake answers BUSY, the same result as the fast check"
        assert spawned == [], "no turn may be spawned over a live user turn"
        self._assert_busy_left_nothing(orch, slot, user_turn)

    @pytest.mark.asyncio
    async def test_user_turn_started_during_admission_read_is_caught_at_publication(
        self, trust_home: Path
    ) -> None:
        """The post-acquisition recheck passes (slot idle); the user turn lands
        during the ``_dashboard_mode_admits`` await, the last await under the
        lock; the publication-time recheck is the one that refuses."""
        from kiro_crew.slack import gateway as gw

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        orch, slot = self._orchestrator(FakeLoopSvc(loop))
        user_turn = object()
        real_admits = gw.GatewayOrchestrator._dashboard_mode_admits

        async def _admits_while_user_sends(a_loop: NudgeLoop, a_slot: Any) -> bool:
            assert not a_slot.running, "the recheck after acquisition saw an idle slot"
            admitted = await real_admits(a_loop, a_slot)
            a_slot.task = user_turn
            a_slot.running = True
            return admitted

        # ``staticmethod`` so the patched attribute is called exactly as the
        # original is: ``self._dashboard_mode_admits(loop, slot)`` with no self.
        with patch.object(
            gw.GatewayOrchestrator, "_dashboard_mode_admits", staticmethod(_admits_while_user_sends)
        ):
            result, spawned = await self._fire(orch, loop)
        assert result is False
        assert spawned == []
        self._assert_busy_left_nothing(orch, slot, user_turn)

    @pytest.mark.asyncio
    async def test_stage_execution_started_during_lock_wait_is_busy_too(
        self, trust_home: Path
    ) -> None:
        """``_in_stage_execution`` is the other half of the predicate: between
        plan stages ``slot.task`` is None and ``running`` is False."""
        import asyncio

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        orch, slot = self._orchestrator(FakeLoopSvc(loop))
        slot.task = None
        held, release = asyncio.Event(), asyncio.Event()
        owner_on = asyncio.create_task(self._hold_lock_until(loop.slot_key, held, release))
        await held.wait()
        fire = asyncio.create_task(self._fire(orch, loop))
        await asyncio.sleep(0)
        assert not fire.done()
        slot._in_stage_execution = True
        release.set()
        await owner_on
        result, spawned = await fire
        assert result is False
        assert spawned == []
        self._assert_busy_left_nothing(orch, slot, None)

    @pytest.mark.asyncio
    async def test_structured_monitor_wake_settles_busy_under_the_lock(
        self, trust_home: Path
    ) -> None:
        """Same race on a structured monitor, landing during the admission read
        so the hook path has already created its admission future: the fire
        returns the typed BUSY at once (the future is settled BUSY, never
        awaited against a turn that was not spawned), and the hook's
        authorization and acceptance never run, so no row is appended."""
        from unittest.mock import MagicMock

        from kiro_crew.monitoring.models import MonitorDispatchResult, MonitorState
        from kiro_crew.slack import gateway as gw

        loop = self._loop()
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
            last_wake_fingerprint="failure-a",
            wake_in_flight=True,
        )
        sa.record_owner_arm(loop.id, loop.slot_key)

        class _MonitorLoopSvc(FakeLoopSvc):
            """``FakeLoopSvc`` plus the three structured-monitor entry points
            ``_monitor_completion_hook`` binds, so this wake builds its
            completion hook and takes the admission-future path."""

            def __init__(self, a_loop: NudgeLoop) -> None:
                super().__init__(a_loop)
                self.record_monitor_turn_completion = AsyncMock()
                self.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
                self.mark_monitor_turn_accepted = MagicMock()

        svc = _MonitorLoopSvc(loop)
        orch, slot = self._orchestrator(svc)
        user_turn = object()
        real_admits = gw.GatewayOrchestrator._dashboard_mode_admits

        async def _admits_while_user_sends(a_loop: NudgeLoop, a_slot: Any) -> bool:
            admitted = await real_admits(a_loop, a_slot)
            a_slot.task = user_turn
            a_slot.running = True
            return admitted

        spawn = MagicMock(side_effect=AssertionError("no turn may be spawned"))
        with (
            patch.object(
                gw.GatewayOrchestrator,
                "_dashboard_mode_admits",
                staticmethod(_admits_while_user_sends),
            ),
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch.object(
                gw, "sel", return_value=SimpleNamespace(log_tool_invocation=lambda **kw: None)
            ),
        ):
            result = await orch._fire_dashboard_nudge(loop, "[Monitor wake]")
        assert result is MonitorDispatchResult.BUSY
        spawn.assert_not_called()
        self._assert_busy_left_nothing(orch, slot, user_turn)
        svc.mark_monitor_turn_accepted.assert_not_called()
        svc.monitor_dispatch_is_authorized.assert_not_awaited()


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
    # Keep the auto-defaulted sentinel out of the real workspace dir AND out of
    # the process-global temp dir: it resolves beneath the running test's own
    # tmp_path (``_sentinel_dir``), so nothing a test creates or removes lives
    # outside its fixture directory.
    sentinel_dir = _SENTINEL_DIR["path"]
    assert sentinel_dir is not None, "_route_patches needs the sentinel_dir fixture"
    stack.enter_context(
        patch(
            "kiro_crew.autonudge_authz.resolve_stop_sentinel",
            lambda slot_key, workspace="default": str(
                sentinel_dir / f".perpetual-test-stop-{slot_key}"
            ),
        )
    )
    return stack


#: The running test's tmp_path, set by the autouse ``_sentinel_dir`` fixture so
#: ``_route_patches`` (35 callers, none of which need to know) can resolve the
#: stop sentinel beneath it.
_SENTINEL_DIR: dict[str, Path | None] = {"path": None}


@pytest.fixture(autouse=True)
def _sentinel_dir(tmp_path: Path) -> Any:
    _SENTINEL_DIR["path"] = tmp_path
    yield tmp_path
    _SENTINEL_DIR["path"] = None


@pytest.fixture
def members_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The members handler's own SEL sink (``handlers.members._sel``)."""
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.members._sel",
        lambda: SimpleNamespace(
            log_tool_invocation=lambda **kw: events.append(kw),
            log_api_access=lambda **kw: None,
        ),
    )
    return events


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
                await resp.read()
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
                await resp.read()
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
                await resp.read()
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
                await resp.read()
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
    async def test_on_with_an_active_unrecorded_loop_repairs_owner_admission(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        members_audit: list[dict[str, Any]],
        trust_home: Path,
    ) -> None:
        running = NudgeLoop(
            id="run00009",
            slot_key="member-scout",
            message="patrol",
            idle_secs=900,
            max_cycles=12,
            active=True,
        )
        svc = FakeLoopSvc(running)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual",
                    json={"member": CREW, "enabled": True},
                )
                await resp.read()
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["loop"]["active"] is True
        assert body["loop"]["max_cycles"] == 0
        assert body["loop"]["max_runtime_secs"] == 0
        assert svc.updates[0]["max_cycles"] == 0
        assert svc.updates[0]["max_runtime_secs"] == 0
        assert sa.is_recorded_owner_arm("run00009", "member-scout") is True
        assert len(members_audit) == 1
        event = members_audit[0]
        assert event["session_key"] == "member-scout"
        assert event["source"] == "dashboard"
        assert event["tool_name"] == "autonudge_start"
        assert event["outcome"] == "invoked"
        assert event["critical"] is True
        metadata = event["metadata"]
        assert isinstance(metadata.pop("caller"), str)
        assert metadata == {
            "slot_key": "member-scout",
            "idle_secs": 900,
            "max_cycles": 12,
            "max_runtime_secs": 0,
            "self_armed": False,
            "owner_armed": True,
        }

    @pytest.mark.asyncio
    async def test_active_owner_repair_refuses_when_critical_audit_fails(
        self, monkeypatch: pytest.MonkeyPatch, trust_home: Path
    ) -> None:
        running = NudgeLoop(
            id="run00010",
            slot_key="member-scout",
            message="patrol",
            idle_secs=900,
            max_cycles=12,
            active=True,
        )
        svc = FakeLoopSvc(running)

        def _fail_audit(**kwargs: Any) -> None:
            raise OSError("audit disk unavailable")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.members._sel",
            lambda: SimpleNamespace(log_tool_invocation=_fail_audit),
        )
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
                await resp.read()
        assert resp.status == 503
        assert (await resp.json())["code"] == "perpetual_on_failed"
        assert sa.is_recorded_owner_arm("run00010", "member-scout") is False

    @pytest.mark.asyncio
    async def test_off_pauses_and_keeps_the_record_with_its_reason(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
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
        sa.record_owner_arm(running.id, running.slot_key)
        sa.record_self_arm("sibling1", "member-other")
        svc = FakeLoopSvc(running)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": False}
                )
                await resp.read()
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
                "expect_fingerprint": None,
                "judge": None,
            }
        ]
        # The AUTHORIZATION went with the pause: the owner entry is the whole
        # of the fire-time admission, and the loop store is agent-writable, so
        # a paused record that kept it could be revived by a forged
        # ``active: true``. Siblings stay.
        assert sa._armed_by_of(running.id, running.slot_key) == ""
        assert sa.is_recorded_self_arm("sibling1", "member-other") is True

    @pytest.mark.asyncio
    async def test_off_reports_a_revoke_that_could_not_be_written_and_restores_the_loop(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path, caplog: Any
    ) -> None:
        """Pause and revoke are ONE transition. A revoke the record refused is
        told to the owner (503) rather than swallowed -- and the pause it
        followed is rolled back, exactly as the member's own stop does: a
        paused row with the owner entry standing is the forge-usable state OFF
        exists to remove, so OFF must not leave one behind on an error the
        owner may never read. The loop is active again, armed as before, its
        stop reason cleared, and no ``ok`` was reported."""
        running = NudgeLoop(
            id="run00002",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            active=True,
            next_due_ts=9_999.0,
        )
        sa.record_owner_arm(running.id, running.slot_key)
        svc = FakeLoopSvc(running)
        with (
            _route_patches(svc),
            patch("kiro_crew.autonudge_selfarm.revoke_arm", side_effect=OSError("disk")),
            caplog.at_level(logging.ERROR, logger="kiro_crew.dashboard.handlers.members"),
        ):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": False}
                )
                await resp.read()
        assert resp.status == 503
        body = await resp.json()
        assert body["code"] == "perpetual_off_failed"
        assert "ok" not in body
        assert "kept running" in body["error"]
        # Paused first, the revoke failed, then the pause was rolled back.
        assert [u["active"] for u in svc.updates] == [False, True]
        assert running.active is True
        assert running.stopped_reason == "" and running.stopped_detail == ""
        assert sa.is_recorded_owner_arm(running.id, running.slot_key) is True
        # The rollback went through the same audited chokepoint as the pause.
        resumes = [
            e
            for e in quiet_authz_audit
            if e.get("tool_name") == "autonudge_update" and e.get("outcome") == "invoked"
        ]
        assert [e["metadata"]["fields"] for e in resumes] == [["active"], ["active"]]
        assert any("restoring the loop" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_off_whose_revoke_and_rollback_both_fail_reports_owner_recovery(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path, caplog: Any
    ) -> None:
        """The revoke is refused and the resume is refused too (the store is
        gone): the row stays paused with the entry standing, and the 503 says
        so -- neither ``ok`` nor a claim that the loop was restored. The owner's
        next OFF meets the paused row on the already-paused path and retries
        the revoke there."""
        running = NudgeLoop(
            id="run00012",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            active=True,
            next_due_ts=9_999.0,
        )
        sa.record_owner_arm(running.id, running.slot_key)

        class ResumeRefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                if kw.get("active") is True:
                    self.updates.append({"loop_id": loop_id, **kw})
                    raise RuntimeError("store unavailable")
                return await FakeLoopSvc.update(self, loop_id, **kw)

        svc = ResumeRefusingSvc(running)
        with (
            _route_patches(svc),
            patch("kiro_crew.autonudge_selfarm.revoke_arm", side_effect=OSError("disk")),
            caplog.at_level(logging.ERROR, logger="kiro_crew.dashboard.handlers.members"),
        ):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": False}
                )
                await resp.read()
        assert resp.status == 503
        body = await resp.json()
        assert body["code"] == "perpetual_off_failed"
        assert "ok" not in body
        assert "could not be restored" in body["error"]
        assert "kept running" not in body["error"]
        assert [u["active"] for u in svc.updates] == [False, True]
        assert running.active is False
        assert running.stopped_reason == "manual"
        assert sa.is_recorded_owner_arm(running.id, running.slot_key) is True
        assert any("could not be restored" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_off_cancelled_during_a_failing_revoke_restores_the_loop(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """The shutdown drain cancels the mutation while the strict revoke is
        in its thread, and the revoke then fails. The cancel must not strand a
        paused row with its admission intact: the revoke is joined, the pause
        is rolled back, and only then does the cancellation propagate."""
        import asyncio
        import threading

        from kiro_crew.dashboard.handlers.members import _perpetual_mutation

        running = NudgeLoop(
            id="run00013",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            active=True,
            next_due_ts=9_999.0,
        )
        sa.record_owner_arm(running.id, running.slot_key)
        svc = FakeLoopSvc(running)
        entered = asyncio.Event()
        release = threading.Event()
        event_loop = asyncio.get_running_loop()

        def _fail_revoke(_loop_id: str) -> None:
            event_loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=2)
            raise OSError("disk")

        state: Any = SimpleNamespace(_slots={"member-scout": _member_slot_obj()})
        with (
            _route_patches(svc),
            patch("kiro_crew.autonudge_selfarm.revoke_arm", side_effect=_fail_revoke),
        ):
            task = asyncio.create_task(
                _perpetual_mutation(
                    state=state,
                    svc=svc,
                    slug=CREW,
                    member=CREW,
                    slot_key="member-scout",
                    enabled=False,
                    caller="t",
                )
            )
            await entered.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert [u["active"] for u in svc.updates] == [False, True]
        assert running.active is True
        assert running.stopped_reason == "" and running.stopped_detail == ""
        assert sa.is_recorded_owner_arm(running.id, running.slot_key) is True

    @pytest.mark.asyncio
    async def test_off_cancelled_during_the_pause_still_revokes_once_the_pause_lands(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """The shutdown drain cancels the mutation while the shielded pause is
        in flight; the pause commits anyway. The entry must not survive over
        the now-paused row -- that is the forge-usable state OFF exists to
        remove -- so the mutation settles the pause and revokes before it
        unwinds."""
        import asyncio

        from kiro_crew.dashboard.handlers.members import _perpetual_mutation

        running = NudgeLoop(
            id="run00009",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            active=True,
            next_due_ts=9_999.0,
        )
        sa.record_owner_arm(running.id, running.slot_key)
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowPauseSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                async def _inner() -> Any:
                    entered.set()
                    await release.wait()
                    return await FakeLoopSvc.update(self, loop_id, **kw)

                return await asyncio.shield(asyncio.ensure_future(_inner()))

        svc = SlowPauseSvc(running)
        state = SimpleNamespace(_slots={"member-scout": _member_slot_obj()})
        with _route_patches(svc):
            task = asyncio.create_task(
                _perpetual_mutation(
                    state=state,
                    svc=svc,
                    slug=CREW,
                    member=CREW,
                    slot_key="member-scout",
                    enabled=False,
                    caller="t",
                )
            )
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()  # waiting for the shielded pause to settle
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert running.active is False
        # The pause landed, so the authorization went with it.
        assert sa._armed_by_of(running.id, running.slot_key) == ""

    @pytest.mark.asyncio
    async def test_off_on_an_already_paused_loop_refuses_the_revoke_when_the_audit_fails(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """AUDIT-OR-DENY: an authorization must not disappear unrecorded. When
        the SEL write fails, the entry stays and the owner is told to retry."""
        paused = NudgeLoop(
            id="run00004",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            active=False,
            stopped_reason="autonudge_stop",
            next_due_ts=0.0,
        )
        sa.record_owner_arm(paused.id, paused.slot_key)
        svc = FakeLoopSvc(paused)

        def _broken(**kw: Any) -> None:
            raise OSError("sel disk full")

        with (
            _route_patches(svc),
            patch(
                "kiro_crew.dashboard.handlers.members._sel",
                return_value=SimpleNamespace(log_tool_invocation=_broken),
            ),
        ):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": False}
                )
                await resp.read()
        assert resp.status == 503
        assert (await resp.json())["code"] == "perpetual_off_failed"
        assert sa.is_recorded_owner_arm(paused.id, paused.slot_key) is True  # still standing
        assert svc.updates == []

    @pytest.mark.asyncio
    async def test_off_on_an_already_paused_loop_still_revokes_the_entry(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        trust_home: Path,
        members_audit: list[dict[str, Any]],
    ) -> None:
        """The loop the member stopped itself (or an earlier OFF paused with a
        revoke that failed) is found paused: nothing to update, but EVERY
        successful OFF ends with the authorization gone, so the entry is
        revoked here too -- and a revoke the record refuses is still a 503."""
        paused = NudgeLoop(
            id="run00003",
            slot_key="member-scout",
            message="perpetual",
            idle_secs=3600,
            max_cycles=0,
            active=False,
            stopped_reason="autonudge_stop",
            next_due_ts=0.0,
        )
        sa.record_owner_arm(paused.id, paused.slot_key)
        svc = FakeLoopSvc(paused)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": False}
                )
                await resp.read()
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["loop"]["active"] is False
        assert body["loop"]["stopped_reason"] == "autonudge_stop"  # the record is kept
        assert svc.updates == []  # nothing to pause
        assert sa._armed_by_of(paused.id, paused.slot_key) == ""
        # The revoke did not escape the audit chokepoint: with no audited update
        # on this path, a critical ``perpetual_revoke`` event names the loop.
        revokes = [e for e in members_audit if e.get("tool_name") == "perpetual_revoke"]
        assert len(revokes) == 1
        assert revokes[0]["critical"] is True
        assert revokes[0]["outcome"] == "invoked"
        assert revokes[0]["metadata"]["loop_id"] == paused.id
        assert revokes[0]["session_key"] == paused.slot_key
        # And the failing spelling of the same step. The pause was not this
        # OFF's (the member's stop made it), so there is nothing to roll back:
        # the row is left exactly as found, reason and all, and no resume is
        # written over the member's stop.
        sa.record_owner_arm(paused.id, paused.slot_key)
        with (
            _route_patches(svc),
            patch("kiro_crew.autonudge_selfarm.revoke_arm", side_effect=OSError("disk")),
        ):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": False}
                )
                await resp.read()
        assert resp.status == 503
        assert (await resp.json())["code"] == "perpetual_off_failed"
        assert svc.updates == []
        assert paused.active is False and paused.stopped_reason == "autonudge_stop"
        assert sa.is_recorded_owner_arm(paused.id, paused.slot_key) is True

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
                await resp.read()
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "loop": None}
        assert svc.updates == [] and svc.added == []

    @pytest.mark.asyncio
    async def test_on_takes_over_an_active_finite_self_arm_and_clears_both_caps(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        members_audit: list[dict[str, Any]],
        trust_home: Path,
    ) -> None:
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
        sa.record_self_arm(finite.id, finite.slot_key)
        svc = FakeLoopSvc(finite)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
                await resp.read()
        assert resp.status == 200
        body = await resp.json()
        assert body["loop"]["max_cycles"] == 0
        assert body["loop"]["max_runtime_secs"] == 0
        assert svc.updates[0]["active"] is True
        assert svc.updates[0]["max_cycles"] == 0
        assert svc.updates[0]["max_runtime_secs"] == 0
        assert sa.is_recorded_owner_arm(finite.id, finite.slot_key) is True
        assert members_audit[0]["tool_name"] == "autonudge_start"

    @pytest.mark.asyncio
    async def test_on_leaves_an_active_unlimited_self_arm_self_owned(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        members_audit: list[dict[str, Any]],
        trust_home: Path,
    ) -> None:
        unlimited = NudgeLoop(
            id="unl00001",
            slot_key="member-scout",
            message="m",
            idle_secs=600,
            max_cycles=0,
            max_runtime_secs=0,
            active=True,
            self_armed=True,
        )
        sa.record_self_arm(unlimited.id, unlimited.slot_key)
        svc = FakeLoopSvc(unlimited)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
                await resp.read()
        assert resp.status == 200
        assert svc.updates == [] and svc.added == []
        assert sa.is_recorded_self_arm(unlimited.id, unlimited.slot_key) is True
        assert members_audit == []

    @pytest.mark.asyncio
    async def test_failed_active_self_takeover_restores_the_self_party(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        members_audit: list[dict[str, Any]],
        trust_home: Path,
    ) -> None:
        finite = NudgeLoop(
            id="fin00002",
            slot_key="member-scout",
            message="m",
            idle_secs=600,
            max_cycles=12,
            max_runtime_secs=7_200,
            active=True,
            self_armed=True,
        )
        sa.record_self_arm(finite.id, finite.slot_key)

        class RefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                self.updates.append({"loop_id": loop_id, **kw})
                return None

        svc = RefusingSvc(finite)
        with _route_patches(svc):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
                await resp.read()
        assert resp.status == 404
        assert sa.is_recorded_self_arm(finite.id, finite.slot_key) is True
        assert members_audit[0]["tool_name"] == "autonudge_start"

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
                    await resp.read()
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
                await resp.read()
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
                await resp.read()
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
                await resp.read()
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
                await resp.read()
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
    async def test_member_cap_waits_for_a_takeover_and_then_sees_the_owner(
        self, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The party check, the loop it judges and the cap write are ONE step
        under the perpetual lock. A takeover that holds the lock first (entry
        rewritten to the owner, loop resumed uncapped) is seen whole: the
        member's cap is refused. Before the fence the member read "self-armed"
        first and its finite cap landed on the owner's Perpetual mode."""
        import asyncio

        from kiro_crew.dashboard import session_directive_apply as sda

        monkeypatch.setattr(
            autonudge_authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None),
        )
        loop = self._loop(self_armed=True)
        sa.record_self_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        held, release = asyncio.Event(), asyncio.Event()

        async def _takeover() -> None:
            async with sa.perpetual_slot_lock(loop.slot_key):
                held.set()
                await release.wait()
                sa.revoke_arm(loop.id)
                sa.record_owner_arm(loop.id, loop.slot_key)
                loop.self_armed = False

        takeover = asyncio.create_task(_takeover())
        await held.wait()
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch.object(sda, "_audit"),
        ):
            member = asyncio.create_task(
                sda._monitor_update(
                    self._state(loop, svc),
                    "dashboard:member-scout",
                    {"patch": {"max_cycles": 24}},
                    self_arm_ok=True,
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not member.done(), "the cap check must wait at the perpetual lock"
            release.set()
            await takeover
            with pytest.raises(sda._DirectiveDenied, match="Perpetual mode"):
                await member
        assert svc.updates == []
        assert loop.max_cycles == 0
        assert "member-scout" not in sa._PERPETUAL_LOCKS

    @pytest.mark.asyncio
    async def test_a_non_member_slot_update_takes_no_perpetual_lock(
        self, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from kiro_crew.dashboard import session_directive_apply as sda

        monkeypatch.setattr(
            autonudge_authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None),
        )
        loop = self._loop(slot_key="chat-7")
        svc = FakeLoopSvc(loop)
        state = SimpleNamespace(
            _slots={"chat-7": SimpleNamespace(mode="", memory_mode="persistent", _app="")}
        )
        # A holder on the slot's lock must NOT delay an ordinary dashboard slot.
        async with sa.perpetual_slot_lock("chat-7"):
            with (
                patch("kiro_crew.autonudge.get_instance", return_value=svc),
                patch.object(sda, "_audit"),
            ):
                result = await asyncio.wait_for(
                    sda._monitor_update(
                        state, "dashboard:chat-7", {"patch": {"max_cycles": 24}}, self_arm_ok=True
                    ),
                    timeout=2,
                )
        assert "updated" in result.lower()
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
        # The record is kept for its words; the authorization is not -- a
        # paused loop with a standing owner entry could be revived by a forged
        # ``active: true`` in the agent-writable store.
        assert sa._armed_by_of(loop.id, loop.slot_key) == ""

    @pytest.mark.asyncio
    async def test_members_own_stop_waits_for_the_owners_takeover_and_then_sees_owner(
        self, trust_home: Path
    ) -> None:
        """The stop arrives while the owner's takeover holds the slot lock,
        between its entry write and its resume. It must not revoke under the
        resume: it waits, then reads the party the takeover wrote and pauses
        the loop the owner's way (record kept, entry revoked AFTER the resume
        landed), so the store never says ON with no entry behind it."""
        import asyncio

        from kiro_crew.dashboard import session_directive_apply as sda
        from kiro_crew.dashboard.handlers.members import _perpetual_lock

        loop = self._loop(active=False, stopped_reason="cycle_cap", next_due_ts=0.0)
        sa.record_self_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        slot = SimpleNamespace(mode="member", _app="")
        order: list[str] = []

        async def _takeover() -> None:
            async with _perpetual_lock(loop.slot_key):
                sa.record_owner_arm(loop.id, loop.slot_key, txn="t1")
                order.append("entry")
                await asyncio.sleep(0.05)  # the window the stop must not enter
                await svc.update(loop.id, active=True, max_cycles=0, max_runtime_secs=0)
                order.append("resumed")

        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            t = asyncio.create_task(_takeover())
            await asyncio.sleep(0.01)  # the takeover holds the lock
            result = await sda._autonudge_stop(slot, "dashboard:member-scout", {"reason": "done"})
            await t
        assert order == ["entry", "resumed"]
        assert "stopped on this session" in result
        # The stop ran AFTER the takeover, saw the owner entry, and took the
        # retain-record path: paused with its words, entry revoked, loop kept.
        assert svc.loop is loop and loop.active is False
        assert loop.stopped_reason == "autonudge_stop"
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == ""

    @pytest.mark.asyncio
    async def test_members_own_stop_rolls_back_when_revoke_is_refused(
        self, trust_home: Path, caplog: Any
    ) -> None:
        """A stop is incomplete while its owner authorization remains.

        The pause rolls back to active, the result reports an error, and the
        directive audit must not claim success.
        """
        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        slot = SimpleNamespace(mode="member", _app="")
        state = self._state(loop, svc)
        audits: list[tuple[str, str, str]] = []
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch("kiro_crew.autonudge_selfarm.revoke_arm", side_effect=OSError("disk")),
            patch.object(sda, "_audit", side_effect=lambda *args: audits.append(args)),
            caplog.at_level(logging.ERROR, logger="kiro_crew.dashboard.session_directive_apply"),
        ):
            result = await sda.apply_session_directive(
                state,
                slot,
                "dashboard:member-scout",
                "autonudge_stop",
                {"reason": "done"},
            )
        assert result.startswith("Error:")
        assert "not stopped" in result
        assert "restored to active" in result
        assert loop.active is True
        assert loop.stopped_reason == ""
        assert loop.stopped_detail == ""
        assert sa.is_recorded_owner_arm(loop.id, loop.slot_key) is True
        assert svc.updates[-1] == {"loop_id": loop.id, "active": True}
        assert audits == [("dashboard:member-scout", "autonudge_stop", "error")]
        assert any("could not be revoked" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_members_own_stop_cancelled_during_the_pause_still_revokes(
        self, trust_home: Path
    ) -> None:
        """The turn is cancelled (slot close, shutdown drain) while the shielded
        pause is in flight; the pause commits anyway. A paused row that kept
        its owner entry is the state the revoke exists to remove, so the stop
        settles the pause, revokes, and only then unwinds."""
        import asyncio

        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowPauseSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                async def _inner() -> Any:
                    entered.set()
                    await release.wait()
                    return await FakeLoopSvc.update(self, loop_id, **kw)

                return await asyncio.shield(asyncio.ensure_future(_inner()))

        svc = SlowPauseSvc(loop)
        slot = SimpleNamespace(mode="member", _app="")
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            task = asyncio.create_task(
                sda._autonudge_stop(slot, "dashboard:member-scout", {"reason": "done"})
            )
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()  # waiting for the shielded pause to settle
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert loop.active is False
        assert loop.stopped_reason == "autonudge_stop"
        # The pause landed, so the authorization went with it.
        assert sa._armed_by_of(loop.id, loop.slot_key) == ""

    @pytest.mark.asyncio
    async def test_cancel_during_failed_revoke_restores_the_loop(self, trust_home: Path) -> None:
        """A cancel cannot strand a paused loop with owner admission intact."""
        import asyncio
        import threading

        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop()
        sa.record_owner_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        slot = SimpleNamespace(mode="member", _app="")
        entered = asyncio.Event()
        release = threading.Event()
        event_loop = asyncio.get_running_loop()

        def _fail_revoke(_loop_id: str) -> None:
            event_loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=2)
            raise OSError("disk")

        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch("kiro_crew.autonudge_selfarm.revoke_arm", side_effect=_fail_revoke),
        ):
            task = asyncio.create_task(
                sda._autonudge_stop(slot, "dashboard:member-scout", {"reason": "done"})
            )
            await entered.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert loop.active is True
        assert loop.stopped_reason == ""
        assert sa.is_recorded_owner_arm(loop.id, loop.slot_key) is True
        assert svc.updates[-1] == {"loop_id": loop.id, "active": True}

    @pytest.mark.asyncio
    async def test_members_stop_cannot_clear_the_owners_pause(self, trust_home: Path) -> None:
        """Owner pressed OFF (row paused ``manual``, entry revoked), then chats
        the crewmate; the turn's ``monitor_stop`` must NOT remove the retained
        row -- removing it would let the same turn's ``monitor_start`` re-arm
        over the owner's pause. Only the owner's switch clears the owner's OFF."""
        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop(active=False, stopped_reason="manual", next_due_ts=0.0)
        svc = FakeLoopSvc(loop)  # no owner entry: OFF revoked it
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        slot = SimpleNamespace(mode="member", _app="")
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            result = await sda._autonudge_stop(slot, "dashboard:member-scout", {"reason": "x"})
        assert removed == []
        assert svc.loop is loop and loop.active is False and loop.stopped_reason == "manual"
        assert "already stopped" in result and "owner's Perpetual mode switch" in result

    @pytest.mark.asyncio
    async def test_members_stop_keeps_the_owners_pause_when_the_record_is_unreadable(
        self, trust_home: Path
    ) -> None:
        """Owner OFF (row ``manual``), then the trust record becomes unreadable.
        Ownership is INDETERMINATE, which the active-loop path treats as the
        owner's (pause, rewrite the reason). A retained pause must be tested
        first: routing it through the owner path would overwrite the owner's
        ``manual`` with the member's ``autonudge_stop``. The row is untouched."""
        from kiro_crew.dashboard import session_directive_apply as sda

        path = sa.self_arm_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        loop = self._loop(active=False, stopped_reason="manual", next_due_ts=0.0)
        svc = FakeLoopSvc(loop)
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        slot = SimpleNamespace(mode="member", _app="")
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            result = await sda._autonudge_stop(slot, "dashboard:member-scout", {"reason": "x"})
        assert removed == [] and svc.updates == []
        assert loop.active is False and loop.stopped_reason == "manual"
        assert loop.stopped_detail == ""
        assert "already stopped" in result
        # The unreadable file is evidence; a stop never touches it.
        assert path.read_text(encoding="utf-8") == "{not json"

    @pytest.mark.asyncio
    async def test_members_stop_revokes_an_owner_entry_left_on_a_paused_row(
        self, trust_home: Path
    ) -> None:
        """An OFF whose revoke was refused leaves a paused ``manual`` row with
        its owner entry standing (and says so). The member's stop keeps the row
        exactly as the owner left it and removes only that entry -- the entry
        plus a forged ``active: true`` in the agent-writable store is a wake."""
        from kiro_crew.dashboard import session_directive_apply as sda

        loop = self._loop(active=False, stopped_reason="manual", next_due_ts=0.0)
        sa.record_owner_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        slot = SimpleNamespace(mode="member", _app="")
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            result = await sda._autonudge_stop(slot, "dashboard:member-scout", {})
        assert removed == [] and svc.updates == []
        assert loop.active is False and loop.stopped_reason == "manual"
        assert "already stopped" in result
        assert sa._armed_by_of(loop.id, loop.slot_key) == ""

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
    async def test_recheck_under_the_lock_reads_a_fresh_configuration(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """The route's own snapshot admitted the member; the crew was deleted
        while the request waited for the lock. The recheck must read the
        configuration AGAIN under the lock and refuse -- a snapshot taken
        before the lock would still show the member and arm a loop for a
        thread nobody owns any more."""
        from kiro_crew.config.loader import KiroCrewConfig

        stopped = NudgeLoop(
            id="old00009", slot_key="member-scout", message="m", idle_secs=60, active=False
        )
        svc = FakeLoopSvc(stopped)
        without_member = KiroCrewConfig()
        loads = [_fake_config(), without_member]
        with (
            _route_patches(svc),
            patch(
                "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
                side_effect=lambda: loads.pop(0),
            ),
        ):
            async with TestClient(
                TestServer(_make_app({"member-scout": _member_slot_obj()}))
            ) as client:
                resp = await client.post(
                    f"/api/members/{CREW}/perpetual", json={"member": CREW, "enabled": True}
                )
                await resp.read()
        assert resp.status == 404
        assert (await resp.json())["code"] == "member_not_found"
        assert loads == []  # both reads happened: the route's and the lock's
        assert svc.updates == [] and svc.added == []
        assert stopped.active is False
        assert not sa.self_arm_record_path().exists()

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
                await resp.read()
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
                await resp.read()
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
                await resp.read()
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
                await resp.read()
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["loop"]["active"] is True and body["loop"]["cycle_count"] == 9
        assert body["loop"]["max_cycles"] == 0 and body["loop"]["max_runtime_secs"] == 0
        # One entry per loop, now the owner's; the stored self_armed bit is inert.
        assert sa._armed_by_of(loop.id, loop.slot_key) == "owner"
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
                await resp.read()
        assert resp.status == 404
        assert (await resp.json())["code"] == "perpetual_on_failed"
        assert loop.active is False and loop.max_cycles == 24
        assert sa._armed_by_of(loop.id, loop.slot_key) == "self"

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
                await resp.read()
        assert resp.status == 404
        assert sa._armed_by_of(loop.id, loop.slot_key) == ""

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
                await resp.read()
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
        assert sa._armed_by_of("unk00040", "member-scout") == ""
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
    async def test_stop_is_refused_and_rolled_back_when_the_record_is_unreadable(
        self, trust_home: Path
    ) -> None:
        """An unreadable record routes to the owner-armed stop (fail closed),
        whose strict ``revoke_arm`` then raises on that same record: the pause
        is rolled back to active, nothing is removed, and the member gets an
        Error rather than a stop that left the authorization standing."""
        from kiro_crew.dashboard import session_directive_apply as sda

        self._corrupt_record(trust_home)
        loop = self._loop()
        svc = FakeLoopSvc(loop)
        removed: list[str] = []

        async def _remove(loop_id: str) -> None:
            removed.append(loop_id)

        svc.remove = _remove  # type: ignore[attr-defined]
        with patch("kiro_crew.autonudge.get_instance", return_value=svc):
            result = await sda._autonudge_stop(_member_slot_obj(), "dashboard:member-scout", {})
        assert result.startswith("Error: auto-nudge loop unk00040 was not stopped")
        assert "restored to active" in result
        assert removed == []
        assert loop.active is True
        assert loop.stopped_reason == "" and loop.stopped_detail == ""

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
    async def test_owner_entry_is_written_after_one_critical_owner_audit(
        self,
        quiet_authz_audit: list[dict[str, Any]],
        members_audit: list[dict[str, Any]],
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        real_record_owner_arm = sa.record_owner_arm

        def _record_owner_arm(loop_id: str, bound_slot_key: str, *, txn: str = "") -> None:
            # The trusted owner entry is the authorization. If the process dies
            # after this call, its critical audit must already be durable.
            assert len(members_audit) == 1
            real_record_owner_arm(loop_id, bound_slot_key, txn=txn)

        monkeypatch.setattr("kiro_crew.autonudge_selfarm.record_owner_arm", _record_owner_arm)
        resumed, error, status = await _takeover_stopped_loop(
            FakeLoopSvc(loop), loop, loop.slot_key, caller="test-owner"
        )

        assert error is None and status == 200 and resumed is loop
        assert len(members_audit) == 1
        event = members_audit[0]
        assert event["tool_name"] == "autonudge_start"
        assert event["outcome"] == "invoked"
        assert event["critical"] is True
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"

    @pytest.mark.asyncio
    async def test_critical_audit_failure_leaves_prior_party_and_loop_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, trust_home: Path
    ) -> None:
        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        svc = FakeLoopSvc(loop)

        def _fail_audit(**kwargs: Any) -> None:
            raise OSError("audit disk unavailable")

        def _unexpected_owner_write(*args: Any, **kwargs: Any) -> None:
            pytest.fail("owner authorization was written after audit failure")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.members._sel",
            lambda: SimpleNamespace(log_tool_invocation=_fail_audit),
        )
        monkeypatch.setattr("kiro_crew.autonudge_selfarm.record_owner_arm", _unexpected_owner_write)
        resumed, error, status = await _takeover_stopped_loop(
            svc, loop, loop.slot_key, caller="test-owner"
        )

        assert resumed is None
        assert error == "audit log unavailable — owner authorization not recorded"
        assert status == 503
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"
        assert svc.updates == []
        assert loop.active is False and loop.max_cycles == 24

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
    async def test_task_cancellation_waits_for_the_resume_and_restores_when_it_fails(
        self, quiet_authz_audit: list[dict[str, Any]], trust_home: Path
    ) -> None:
        """A cancellation of the takeover TASK itself (gateway shutdown) lands
        while the shielded resume is in flight. The takeover does not unwind on
        the spot: it waits for the resume to settle, and when the loop did NOT
        resume (here: the service refused) the entry goes back to its prior
        party before the cancel propagates -- an owner entry over a loop that
        never resumed is the same forge-usable state as the pre-resume case.
        Request-level cancellation never reaches here (the handler shields the
        task)."""
        import asyncio

        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowRefusingSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                async def _inner() -> Any:
                    entered.set()
                    await release.wait()
                    return None  # refused: the loop stays stopped

                return await asyncio.shield(asyncio.ensure_future(_inner()))

        svc = SlowRefusingSvc(loop)
        task = asyncio.create_task(_takeover_stopped_loop(svc, loop, "member-scout", caller="t"))
        await entered.wait()
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "owner"
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()  # waiting for the shielded resume to settle
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"
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
        commit AFTER the cancel; the takeover waits for it and, seeing the loop
        resumed, leaves the owner entry the running loop now needs."""
        import asyncio

        from kiro_crew.dashboard.handlers.members import _takeover_stopped_loop

        loop = _takeover_fixture_loop()
        sa.record_self_arm(loop.id, loop.slot_key)
        entered, release = asyncio.Event(), asyncio.Event()

        class CommitAfterCancelSvc(FakeLoopSvc):
            async def update(self, loop_id: str, **kw: Any) -> Any:
                async def _inner() -> Any:
                    entered.set()
                    await release.wait()
                    return await FakeLoopSvc.update(self, loop_id, **kw)  # lands late

                return await asyncio.shield(asyncio.ensure_future(_inner()))

        svc = CommitAfterCancelSvc(loop)
        task = asyncio.create_task(_takeover_stopped_loop(svc, loop, "member-scout", caller="t"))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and loop.active is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert loop.active is True
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


class TestRevokeArm:
    def test_revoke_drops_the_entry_and_keeps_siblings(self, trust_home: Path) -> None:
        sa.record_owner_arm("rv000001", "member-scout")
        sa.record_self_arm("rv000002", "member-other")
        sa.revoke_arm("rv000001")
        assert sa._armed_by_of("rv000001", "member-scout") == ""
        assert sa.is_recorded_self_arm("rv000002", "member-other") is True
        sa.revoke_arm("rv000001")  # idempotent

    def test_revoke_is_strict_where_forget_is_best_effort(self, trust_home: Path) -> None:
        sa.record_owner_arm("rv000003", "member-scout")
        sa.self_arm_record_path().write_text("{not json", encoding="utf-8")
        with pytest.raises(OSError):
            sa.revoke_arm("rv000003")
        sa.forget_self_arm("rv000003")  # logs, never raises
        assert sa.self_arm_record_path().read_text(encoding="utf-8") == "{not json"


class TestStrictRecord:
    def test_present_but_malformed_entry_raises(self, trust_home: Path) -> None:
        import json

        sa.record_self_arm("good0001", "member-a")
        path = sa.self_arm_record_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["loops"]["str00001"] = "not a dict"
        data["loops"]["who00001"] = {"slot_key": "member-a", "armed_by": "someone"}
        data["loops"]["nos00001"] = {"armed_by": "self"}
        data["seal"] = sa._seal(data["loops"])
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
        assert sa._armed_by_of("who00001", "member-a") == ""

    def test_write_preserves_malformed_siblings_verbatim(self, trust_home: Path) -> None:
        import json

        sa.record_self_arm("sibl0001", "member-a")
        path = sa.self_arm_record_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["loops"]["odd00001"] = "kept as is"
        data["loops"]["odd00002"] = {"slot_key": 7, "armed_by": "self"}
        data["seal"] = sa._seal(data["loops"])
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
        assert _entry("tok00001", "member-a") == ("owner", "abc")
        assert sa.read_arm_party_strict("tok00001", "member-a") == "owner"
        # Arm-time entries carry none; self entries carry none.
        sa.record_owner_arm("tok00002", "member-a")
        sa.record_self_arm("tok00003", "member-a")
        assert _entry("tok00002", "member-a") == ("owner", "")
        assert _entry("tok00003", "member-a") == ("self", "")
        assert _entry("tok00001", "member-b") == ("", "")

    def test_restore_touches_only_its_own_token(self, trust_home: Path) -> None:
        from kiro_crew.dashboard.handlers.members import _restore_arm_party

        # Takeover A wrote token "aaa"; takeover B then wrote "bbb".
        sa.record_owner_arm("tok00010", "member-a", txn="aaa")
        sa.record_owner_arm("tok00010", "member-a", txn="bbb")
        _restore_arm_party("tok00010", "member-a", "self", "aaa")
        assert _entry("tok00010", "member-a") == ("owner", "bbb")
        # B's own late rollback still works.
        _restore_arm_party("tok00010", "member-a", "self", "bbb")
        assert _entry("tok00010", "member-a") == ("self", "")
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
        assert _entry(loop.id, loop.slot_key) == ("owner", "next-takeover")


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
        assert _entry("atm00001", "member-a") == ("owner", "bbb")

    def test_match_restores_self_or_removes(self, trust_home: Path) -> None:
        sa.record_owner_arm("atm00002", "member-a", txn="t2")
        assert sa.restore_arm_party_if_token("atm00002", "member-a", "t2", "self") is True
        assert _entry("atm00002", "member-a") == ("self", "")
        sa.record_owner_arm("atm00003", "member-a", txn="t3")
        assert sa.restore_arm_party_if_token("atm00003", "member-a", "t3", "") is True
        assert _entry("atm00003", "member-a") == ("", "")
        # Nothing to restore: blank token, owner prior party, another slot, no entry.
        sa.record_owner_arm("atm00004", "member-a", txn="t4")
        assert sa.restore_arm_party_if_token("atm00004", "member-a", "", "self") is False
        assert sa.restore_arm_party_if_token("atm00004", "member-a", "t4", "owner") is False
        assert sa.restore_arm_party_if_token("atm00004", "member-b", "t4", "self") is False
        assert sa.restore_arm_party_if_token("absent99", "member-a", "t4", "self") is False
        assert _entry("atm00004", "member-a") == ("owner", "t4")

    def test_siblings_survive_the_locked_rewrite_verbatim(self, trust_home: Path) -> None:
        import json

        sa.record_self_arm("sib00001", "member-a")
        sa.record_owner_arm("sib00002", "member-b", txn="other")
        sa.record_owner_arm("tgt00001", "member-c", txn="mine")
        path = sa.self_arm_record_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["loops"]["odd00001"] = "kept as is"
        data["seal"] = sa._seal(data["loops"])
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
        data["seal"] = sa._seal(data["loops"])
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
        assert _entry("thin0001", "member-a") == ("self", "")
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
            # Cancelled, but not yet finished: the join is waiting on the thread,
            # and the request -- which only awaits the shielded task -- waits
            # with it (a shield resolves when the task ends, not when it is
            # asked to).
            await asyncio.sleep(0.05)
            assert not supervised.done()
            assert not request_task.done()
            assert mh._PERPETUAL_LOCKS["member-scout"].locked()
            assert finished == []
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await supervised
            with pytest.raises(asyncio.CancelledError):
                await request_task
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
            # The join holds the task open until the thread ends; the request,
            # awaiting only the shielded task, stays open with it.
            await asyncio.sleep(0.05)
            assert not supervised.done() and finished == []
            assert not request_task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await supervised
            with pytest.raises(asyncio.CancelledError):
                await request_task
        assert finished == ["done"]
        # Task-level cancel BEFORE the resume was issued: the owner write landed
        # and was then put back to the prior party (self), because a loop the
        # store shows OFF must not sit under an owner entry a forged
        # ``active: true`` could ride. The loop itself is untouched.
        assert sa.read_arm_party_strict(loop.id, loop.slot_key) == "self"
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


class TestAuthorizerCancelDuringAdd:
    """The service shields its persist, so an add can commit AFTER the arm is
    cancelled. The trust record must follow the store: entry kept when the
    loop landed, forgotten when it did not."""

    @staticmethod
    def _svc(commit: bool, entered: Any, release: Any) -> Any:
        import asyncio

        class SlowAddSvc(RecordingSvc):
            def __init__(self) -> None:
                super().__init__()
                self._committed: Any = None

            def get_by_id(self, loop_id: str) -> Any:
                return (
                    self._committed if self._committed and self._committed.id == loop_id else None
                )

            async def add(self, **kw: Any) -> Any:
                async def _inner() -> Any:
                    entered.set()
                    await release.wait()
                    if not commit:
                        raise RuntimeError("store refused")
                    self._committed = NudgeLoop(
                        id=kw["loop_id"],
                        slot_key=kw["slot_key"],
                        message=kw["message"],
                        idle_secs=kw["idle_secs"],
                        max_cycles=kw["max_cycles"],
                        active=True,
                        next_due_ts=4_000.0,
                    )
                    self.added.append(kw)
                    return self._committed

                # The real service shields its inner persist the same way.
                return await asyncio.shield(asyncio.ensure_future(_inner()))

        return SlowAddSvc()

    async def _arm_then_cancel(self, svc: Any, entered: Any, release: Any, tmp_path: Path) -> None:
        import asyncio

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
        await asyncio.sleep(0.02)
        assert not task.done()  # waiting for the shielded add to settle
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_add_that_commits_after_the_cancel_keeps_its_entry(
        self, audits: list[dict[str, Any]], trust_home: Path, tmp_path: Path
    ) -> None:
        import asyncio

        entered, release = asyncio.Event(), asyncio.Event()
        svc = self._svc(True, entered, release)
        await self._arm_then_cancel(svc, entered, release, tmp_path)
        assert len(svc.added) == 1
        loop_id = svc.added[0]["loop_id"]
        # The loop is in the store, so its authorization stays: no ON reading
        # over a loop every wake would refuse.
        assert sa.is_recorded_owner_arm(loop_id, "member-scout") is True

    @pytest.mark.asyncio
    async def test_add_that_fails_after_the_cancel_forgets_its_entry(
        self, audits: list[dict[str, Any]], trust_home: Path, tmp_path: Path
    ) -> None:
        import asyncio

        entered, release = asyncio.Event(), asyncio.Event()
        svc = self._svc(False, entered, release)
        await self._arm_then_cancel(svc, entered, release, tmp_path)
        assert svc.added == []
        assert sa._read_record() == {}


class TestAuthorizerCancelDuringMonitorAdd:
    """The structured-monitor branch shields its add the same way as the
    plain-loop branch: a cancel waits for ``add_monitor`` to settle and the
    self-arm entry follows the store, so a monitor that commits after the
    cancel is not stranded without the record its every wake is admitted by."""

    @staticmethod
    def _monitor() -> Any:
        from kiro_crew.monitoring.models import MonitorBudgets, MonitorState

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

    @staticmethod
    def _svc(commit: bool, entered: Any, release: Any) -> Any:
        import asyncio

        class SlowMonitorSvc(RecordingSvc):
            def __init__(self) -> None:
                super().__init__()
                self.added_monitors: list[dict[str, Any]] = []
                self._committed: Any = None

            def get_by_id(self, loop_id: str) -> Any:
                return (
                    self._committed if self._committed and self._committed.id == loop_id else None
                )

            async def add_monitor(self, **kw: Any) -> Any:
                async def _inner() -> Any:
                    entered.set()
                    await release.wait()
                    if not commit:
                        raise RuntimeError("store refused")
                    self._committed = SimpleNamespace(id=kw["loop_id"], slot_key=kw["slot_key"])
                    self.added_monitors.append(kw)
                    return self._committed

                return await asyncio.shield(asyncio.ensure_future(_inner()))

        return SlowMonitorSvc()

    async def _arm_then_cancel(self, svc: Any, entered: Any, release: Any) -> None:
        import asyncio

        task = asyncio.create_task(
            authorize_and_add_nudge(
                svc=svc,
                state=_state({"member-scout": _slot("member")}),
                slot_key="member-scout",
                message="structured monitor",
                source="mcp-directive",
                monitor=self._monitor(),
                initiator_slot_key="member-scout",
            )
        )
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()  # waiting for the shielded add_monitor to settle
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_monitor_that_commits_after_the_cancel_keeps_its_entry(
        self, audits: list[dict[str, Any]], trust_home: Path
    ) -> None:
        import asyncio

        entered, release = asyncio.Event(), asyncio.Event()
        svc = self._svc(True, entered, release)
        await self._arm_then_cancel(svc, entered, release)
        assert len(svc.added_monitors) == 1
        assert svc.added_monitors[0]["self_armed"] is True
        loop_id = svc.added_monitors[0]["loop_id"]
        assert sa.is_recorded_self_arm(loop_id, "member-scout") is True

    @pytest.mark.asyncio
    async def test_monitor_that_fails_after_the_cancel_forgets_its_entry(
        self, audits: list[dict[str, Any]], trust_home: Path
    ) -> None:
        import asyncio

        entered, release = asyncio.Event(), asyncio.Event()
        svc = self._svc(False, entered, release)
        await self._arm_then_cancel(svc, entered, release)
        assert svc.added_monitors == []
        assert sa._read_record() == {}


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
        # The write landed and the add never ran -- and the entry did NOT stay:
        # an owner entry with no loop behind it would vouch for a forged loop
        # of that id on this slot, so the cancel path revokes what it wrote
        # before it propagates.
        assert svc.added == []
        assert sa._read_record() == {}

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


class TestRosterPerpetualReading:
    """``GET /api/members`` carries ``perpetual`` per row, read from the live
    registry: on / off / none, so the roster and the team view can show a
    paused crewmate without a second request. ``perpetual_state_of`` is the one
    spelling of that reading."""

    @staticmethod
    def _svc(loop: NudgeLoop | None) -> Any:
        return SimpleNamespace(get_by_slot=lambda slot_key: loop)

    def test_admitted_active_loop_reads_on(self, trust_home: Path) -> None:
        from kiro_crew.dashboard.handlers.members import perpetual_state_of

        loop = NudgeLoop(id="ro000001", slot_key="member-scout", message="m", idle_secs=60)
        sa.record_self_arm(loop.id, loop.slot_key)
        assert perpetual_state_of(self._svc(loop), "member-scout") == "on"

    def test_preloaded_parties_keep_roster_rows_in_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers.members import perpetual_state_of

        loop = NudgeLoop(id="ro000006", slot_key="member-scout", message="m", idle_secs=60)

        def _unexpected_read(loop_id: str, slot_key: str) -> str:
            raise AssertionError("per-row trust-record read")

        monkeypatch.setattr(sa, "read_arm_party_strict", _unexpected_read)
        parties = {("ro000006", "member-scout"): "owner"}
        assert perpetual_state_of(self._svc(loop), "member-scout", arm_parties=parties) == "on"

    def test_active_loop_without_a_valid_record_reads_off(self, trust_home: Path) -> None:
        from kiro_crew.dashboard.handlers.members import perpetual_state_of

        loop = NudgeLoop(id="ro000005", slot_key="member-scout", message="m", idle_secs=60)
        assert perpetual_state_of(self._svc(loop), "member-scout") == "off"

    def test_paused_loop_reads_off(self) -> None:
        from kiro_crew.dashboard.handlers.members import perpetual_state_of

        loop = NudgeLoop(
            id="ro000002",
            slot_key="member-scout",
            message="m",
            idle_secs=60,
            active=False,
            stopped_reason="manual",
        )
        assert perpetual_state_of(self._svc(loop), "member-scout") == "off"

    def test_no_record_no_slot_or_no_service_reads_none(self) -> None:
        from kiro_crew.dashboard.handlers.members import perpetual_state_of

        assert perpetual_state_of(self._svc(None), "member-scout") == "none"
        loop = NudgeLoop(id="ro000003", slot_key="member-scout", message="m", idle_secs=60)
        assert perpetual_state_of(self._svc(loop), "") == "none"
        assert perpetual_state_of(None, "member-scout") == "none"

    def test_structured_monitor_is_not_the_switchs_loop(self) -> None:
        from kiro_crew.dashboard.handlers.members import perpetual_state_of

        loop = NudgeLoop(id="ro000004", slot_key="member-scout", message="m", idle_secs=60)
        with patch("kiro_crew.autonudge.is_structured_monitor_loop", return_value=True):
            assert perpetual_state_of(self._svc(loop), "member-scout") == "none"


# ── (h) the record's own masked leaf ────────────────────────────────────────


class TestArmRecordLeaf:
    """Where the record lives is the whole of its authority, so pin all three
    dispositions against the CODE, not a wording: the file-tool fence, the OS
    sandbox mask, and the sandbox-visible set the record must NOT sit under --
    and pin the SHAPE of the mask: an enclosing whole-directory stand-in that
    holds the name for the namespace lifetime, not a leaf of its own at the
    data-home root that a host-side replace could swap out from under."""

    def test_the_record_sits_inside_the_tag_grants_stand_in(self, trust_home: Path) -> None:
        from kiro_crew.dashboard import chat_tag_grants

        path = sa.self_arm_record_path()
        assert path.name == sa.SELF_ARM_RECORD_NAME
        assert path.parent == trust_home / sa.ARM_RECORD_HOST_DIRNAME / sa.ARM_RECORD_DIRNAME
        # A child of the EXISTING grant-store root, which is a direct child of the
        # data home; the record's own directory is never a root-level leaf.
        assert path.parent.parent == trust_home / sa.ARM_RECORD_HOST_DIRNAME
        assert path.parent.parent.parent == trust_home
        assert sa.ARM_RECORD_LEAF == f"{sa.ARM_RECORD_HOST_DIRNAME}/{sa.ARM_RECORD_DIRNAME}"
        # The host IS the chat_tag grant store's root -- spelled, not imported, so
        # this module never loads token_secret on import; the pin keeps them equal.
        assert sa.ARM_RECORD_HOST_DIRNAME == chat_tag_grants._STORE_SUBDIR
        # Its own files stay siblings of the record's directory, never inside it.
        assert chat_tag_grants._store_path().parent.name == sa.ARM_RECORD_HOST_DIRNAME
        assert sa._LEGACY_DIRNAME not in path.parts

    def test_the_record_is_fenced_from_agent_file_tools(self) -> None:
        """The fence is prefix-matched, so the host's entry covers the record; a
        second entry for the child would be a redundant name to keep in step."""
        from kiro_crew import security

        assert sa.ARM_RECORD_HOST_DIRNAME in security._CREW_SECRET_LEAVES
        assert sa.ARM_RECORD_DIRNAME not in security._CREW_SECRET_LEAVES
        assert sa.ARM_RECORD_LEAF not in security._CREW_SECRET_LEAVES

    def test_the_record_is_held_by_an_enclosing_stand_in_mask(self) -> None:
        """The write path the file-tool fence cannot see: a command that builds
        the path at runtime. ``trust/`` is a declared sandbox read-write
        exception, which is why the record is not under it.

        The mask that holds the record is the HOST's: a whole-directory stand-in
        pre-created before every spawn. The record's own nested leaf is listed so
        the launcher payload names the path and ``test_sandbox_protected_name_holds``
        can pin the ancestor hold (``HELD_BY_ENCLOSING_MASK``); it is NOT
        pre-created (the materialiser refuses intermediate directories, and an
        absent child of a masked host is invisible either way) and NOT in
        ``_CREW_NO_ALIAS_LEAVES`` (a link at the host refuses the spawn as every
        masked leaf does). A root-level ``autonudge-trust`` leaf must never come
        back: it would be held only at spawn.
        """
        from kiro_crew import sandbox

        assert sa.ARM_RECORD_HOST_DIRNAME in sandbox._CREW_HIDDEN_LEAVES
        assert sa.ARM_RECORD_HOST_DIRNAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert sa.ARM_RECORD_LEAF in sandbox._CREW_HIDDEN_LEAVES
        assert sa.ARM_RECORD_DIRNAME not in sandbox._CREW_HIDDEN_LEAVES
        assert sa.ARM_RECORD_DIRNAME not in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert sa.ARM_RECORD_LEAF not in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert sa.ARM_RECORD_DIRNAME not in sandbox._CREW_NO_ALIAS_LEAVES
        assert sa.ARM_RECORD_LEAF not in sandbox._CREW_NO_ALIAS_LEAVES
        for name in (sa.ARM_RECORD_HOST_DIRNAME, sa.ARM_RECORD_DIRNAME, sa.ARM_RECORD_LEAF):
            assert name not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
            assert name not in sandbox._CREW_READONLY_LEAVES
        assert sa._LEGACY_DIRNAME in sandbox._CREW_SANDBOX_VISIBLE_LEAVES  # the reason

    @pytest.mark.skipif(os.name != "posix", reason="symlink planting is POSIX-shaped here")
    def test_a_link_at_the_host_or_the_child_refuses_the_arm_and_is_never_followed(
        self, trust_home: Path
    ) -> None:
        """A link at either directory fails the arm CLOSED and writes nothing through it.

        The refusal is the open itself (``pin_directory``: ``O_DIRECTORY | O_NOFOLLOW``),
        not a link check taken first and trusted after, so there is no window in
        which a link planted between the two is followed. Nothing is removed either:
        the link is left for the operator (or the grant store's own boot pass) to
        deal with, and the target it points at gains no directory, no lock and no
        record.
        """
        elsewhere = trust_home / "elsewhere"
        elsewhere.mkdir()
        host = trust_home / sa.ARM_RECORD_HOST_DIRNAME
        host.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(OSError):
            sa.record_self_arm("link0001", "member-a")
        assert host.is_symlink()  # refused, not replaced
        assert list(elsewhere.iterdir()) == []
        assert sa.is_recorded_self_arm("link0001", "member-a") is False

        host.unlink()
        sa.record_self_arm("real0001", "member-a")
        child = host / sa.ARM_RECORD_DIRNAME
        assert host.is_dir() and not host.is_symlink()
        assert child.is_dir() and not child.is_symlink()
        assert stat.S_IMODE(host.stat().st_mode) == 0o700
        assert stat.S_IMODE(child.stat().st_mode) == 0o700
        assert sorted(p.name for p in child.iterdir()) == sorted(
            [sa.SELF_ARM_RECORD_NAME, sa._LOCK_NAME]
        )

        # A link planted at the CHILD alone is refused the same way, and the
        # sibling entry already recorded is untouched.
        shutil.rmtree(child)
        child.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(OSError):
            sa.record_self_arm("link0002", "member-b")
        assert child.is_symlink()
        assert list(elsewhere.iterdir()) == []
        assert sa.is_recorded_self_arm("link0002", "member-b") is False

    def test_the_module_docstring_describes_the_live_layout(self) -> None:
        doc = sa.__doc__ or ""
        assert doc
        assert f"``{sa.ARM_RECORD_LEAF}/``" in doc
        assert "under the keystone-gated ``trust/``" not in doc
        assert "directory of its own under the data" not in doc


class TestLegacyRecordRetirement:
    """A record left under ``trust/`` is deleted, never carried over: that
    directory is writable from the sandbox, so its entries may be forged and
    cannot be told from real ones after the fact."""

    def _legacy(self, home: Path, entries: dict[str, Any]) -> Path:
        legacy = home / sa._LEGACY_DIRNAME / sa.SELF_ARM_RECORD_NAME
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"version": 1, "loops": entries}), encoding="utf-8")
        (legacy.parent / sa._LOCK_NAME).write_text("", encoding="utf-8")
        return legacy

    def test_a_reader_discards_the_legacy_file_and_vouches_for_nothing_in_it(
        self, trust_home: Path, caplog: Any
    ) -> None:
        legacy = self._legacy(
            trust_home, {"old00001": {"slot_key": "member-scout", "armed_ts": 1.0}}
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge_selfarm"):
            assert sa.is_recorded_self_arm("old00001", "member-scout") is False
        assert not legacy.exists()
        assert not (legacy.parent / sa._LOCK_NAME).exists()
        assert not sa.self_arm_record_path().exists()
        # The strict reader answers "nothing recorded" too, not an error.
        assert sa.read_arm_party_strict("old00001", "member-scout") == ""
        assert any("must be armed again" in rec.message for rec in caplog.records)

    def test_a_writer_discards_it_under_its_lock_and_records_only_its_own_entry(
        self, trust_home: Path
    ) -> None:
        legacy = self._legacy(
            trust_home, {"old00001": {"slot_key": "member-scout", "armed_ts": 1.0}}
        )
        sa.record_owner_arm("new00001", "member-other")
        assert not legacy.exists()
        assert sa.is_recorded_self_arm("old00001", "member-scout") is False
        assert sa.is_recorded_owner_arm("new00001", "member-other") is True
        assert set(sa._read_record()) == {"new00001"}

    def test_a_legacy_file_appearing_beside_a_live_record_is_discarded_too(
        self, trust_home: Path
    ) -> None:
        sa.record_self_arm("new00001", "member-scout")
        legacy = self._legacy(
            trust_home, {"old00001": {"slot_key": "member-scout", "armed_ts": 1.0}}
        )
        sa.record_self_arm("new00002", "member-other")
        assert not legacy.exists()
        assert sa.is_recorded_self_arm("old00001", "member-scout") is False
        assert sa.is_recorded_self_arm("new00001", "member-scout") is True
        assert sa.is_recorded_self_arm("new00002", "member-other") is True

    def test_a_discard_that_fails_is_logged_and_reads_as_not_recorded(
        self, trust_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        self._legacy(trust_home, {"old00001": {"slot_key": "member-scout", "armed_ts": 1.0}})
        real_unlink = Path.unlink

        def _refuse(self: Path, *args: Any, **kwargs: Any) -> None:
            if self.name == sa.SELF_ARM_RECORD_NAME:
                raise OSError("busy")
            real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _refuse)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge_selfarm"):
            assert sa.is_recorded_self_arm("old00001", "member-scout") is False
        assert any("could not discard" in rec.message for rec in caplog.records)

    def test_no_legacy_file_is_a_no_op(self, trust_home: Path) -> None:
        sa._retire_legacy_record()
        assert not (trust_home / sa._LEGACY_DIRNAME).exists()
        assert not sa.self_arm_record_path().exists()


class TestRecordSeal:
    """The record is sealed under the gateway's token secret. The directory
    mask closes the leaf from the boot that first masks it; bytes planted at
    the same path BEFORE that boot -- when the name was an ordinary writable
    child of the data home -- are told apart by the seal, which a plant could
    not mint. Unsealed content is nobody's: readers refuse it, the next writer
    moves it aside."""

    @staticmethod
    def _plant(home: Path, entries: dict[str, Any], **extra: Any) -> Path:
        path = sa.self_arm_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "loops": entries, **extra}), encoding="utf-8")
        return path

    def test_a_written_record_carries_a_verifying_seal(self, trust_home: Path) -> None:
        sa.record_owner_arm("seal0001", "member-scout")
        raw = json.loads(sa.self_arm_record_path().read_text(encoding="utf-8"))
        assert raw["version"] == 2
        assert raw["seal"] == sa._seal(raw["loops"])
        assert sa.is_recorded_owner_arm("seal0001", "member-scout") is True
        assert sa.read_arm_party_strict("seal0001", "member-scout") == "owner"

    def test_a_pre_upgrade_plant_vouches_for_nobody(self, trust_home: Path, caplog: Any) -> None:
        """The exact attack: a matching loop and an owner entry planted before
        the leaf was masked. Both readers answer "nothing recorded" -- the
        strict one WITHOUT raising, since an unsealed file is a certain answer,
        not an unreadable one -- and neither reader touches the file."""
        planted = self._plant(
            trust_home,
            {"plnt0001": {"slot_key": "member-scout", "armed_ts": 1.0, "armed_by": "owner"}},
        )
        before = planted.read_text(encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge_selfarm"):
            assert sa.is_recorded_owner_arm("plnt0001", "member-scout") is False
            assert sa.is_recorded_self_arm("plnt0001", "member-scout") is False
            assert sa.read_arm_party_strict("plnt0001", "member-scout") == ""
        assert any("no valid seal" in rec.message for rec in caplog.records)
        assert planted.read_text(encoding="utf-8") == before

    def test_a_plant_with_a_self_minted_seal_is_still_refused(self, trust_home: Path) -> None:
        entries = {"plnt0002": {"slot_key": "member-scout", "armed_ts": 1.0, "armed_by": "owner"}}
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        forged = hmac.new(b"not-the-gateway-key", sa._SEAL_DOMAIN + canonical, hashlib.sha256)
        self._plant(trust_home, entries, seal=forged.hexdigest())
        assert sa.is_recorded_owner_arm("plnt0002", "member-scout") is False
        assert sa.read_arm_party_strict("plnt0002", "member-scout") == ""

    def test_the_next_writer_quarantines_the_plant_and_records_only_its_own(
        self, trust_home: Path, caplog: Any
    ) -> None:
        planted = self._plant(
            trust_home,
            {"plnt0003": {"slot_key": "member-scout", "armed_ts": 1.0, "armed_by": "owner"}},
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge_selfarm"):
            sa.record_owner_arm("real0001", "member-other")
        aside = planted.with_name(planted.name + sa._UNSEALED_SUFFIX)
        assert aside.is_file() and "plnt0003" in aside.read_text(encoding="utf-8")
        assert set(sa._read_record()) == {"real0001"}
        assert sa.is_recorded_owner_arm("plnt0003", "member-scout") is False
        assert sa.is_recorded_owner_arm("real0001", "member-other") is True
        assert any("moved aside" in rec.message for rec in caplog.records)
        # A second plant replaces the first quarantine; the directory does not grow.
        sa.self_arm_record_path().write_text(json.dumps({"version": 1, "loops": {}}))
        sa.record_self_arm("real0002", "member-third")
        assert "plnt0003" not in aside.read_text(encoding="utf-8")
        assert sorted(p.name for p in planted.parent.iterdir()) == sorted(
            [planted.name, aside.name, sa._LOCK_NAME]
        )

    def test_tampering_a_sealed_record_in_place_refuses_it_whole(self, trust_home: Path) -> None:
        """The seal covers every entry: flipping one slot_key -- or adding one
        entry -- without the key invalidates the honest siblings too. Refusing
        is the safe side; the loops are armed again."""
        sa.record_owner_arm("hon00001", "member-a")
        sa.record_self_arm("hon00002", "member-b")
        path = sa.self_arm_record_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["loops"]["hon00001"]["slot_key"] = "member-victim"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert sa.is_recorded_owner_arm("hon00001", "member-victim") is False
        assert sa.is_recorded_self_arm("hon00002", "member-b") is False

    def test_a_rotated_token_key_breaks_every_seal_and_the_writer_starts_clean(
        self, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sa.record_owner_arm("rot00001", "member-scout")
        assert sa.is_recorded_owner_arm("rot00001", "member-scout") is True
        monkeypatch.setattr(sa, "_seal_secret", lambda: b"rotated-key-bytes")
        assert sa.is_recorded_owner_arm("rot00001", "member-scout") is False
        assert sa.read_arm_party_strict("rot00001", "member-scout") == ""
        sa.record_owner_arm("rot00002", "member-scout")
        assert set(sa._read_record()) == {"rot00002"}
        assert sa.is_recorded_owner_arm("rot00002", "member-scout") is True

    def test_a_key_that_cannot_be_loaded_refuses_rather_than_raises(
        self, trust_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        sa.record_owner_arm("key00001", "member-scout")

        def _boom() -> bytes:
            raise OSError("key store unavailable")

        monkeypatch.setattr(sa, "_seal_secret", _boom)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge_selfarm"):
            assert sa.is_recorded_owner_arm("key00001", "member-scout") is False
        assert any("could not be checked" in rec.message for rec in caplog.records)

    def test_a_file_that_does_not_parse_is_left_for_the_strict_reader(
        self, trust_home: Path
    ) -> None:
        """Only a PARSEABLE unsealed file is quarantined. A torn write of the
        gateway's own does not parse, and the strict reader's refusal keeps it
        as evidence exactly as before."""
        path = sa.self_arm_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(OSError):
            sa.record_owner_arm("torn0001", "member-scout")
        assert path.read_text(encoding="utf-8") == "{not json"
        assert not path.with_name(path.name + sa._UNSEALED_SUFFIX).exists()

    def test_the_seal_key_is_the_dashboard_token_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard import token_secret

        monkeypatch.setattr(token_secret, "_get_secret", lambda: b"token-signing-bytes")
        assert sa._seal_secret() == b"token-signing-bytes"
