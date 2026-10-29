"""The spawn gate's macOS kernel memory-pressure hold (``subagent.py``).

``TestSpawnAdmissionGatePressureHold`` covers a root start that waits in the capacity
window while the kernel reports pressure and a dedicated runtime of ours is running: among
the cases, which starts the hold lets through, its bound, the per-lane progress clocks, the
chronic episode and the wait label a held row carries. Besides its own helper methods it
inherits the ones ``TestSpawnAdmissionGate`` also uses from
``_TestSpawnAdmissionGateHarness`` in ``admission_gate_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from admission_gate_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _await_queued_event,
    _close_managers,
    _TestSpawnAdmissionGateHarness,
    _wait_for,
)
from overload_fakes import wait_taskq_open

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS

#: The macOS pressure hold's bound under the default config: the
#: ``agent.subagent_queue_max_wait_secs`` default, which a fresh manager boots on.
_HOLD_BOUND_SECS = float(DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS)


class TestSpawnAdmissionGatePressureHold(_TestSpawnAdmissionGateHarness):
    """The macOS kernel memory-pressure hold inside the memory floor."""

    # ── macOS kernel memory pressure: a hold inside the memory floor ─────────
    #
    # The reclaimable figure cleared the floor; the kernel says WARN or worse.
    # A root start waits in the capacity window while a dedicated runtime of
    # ours is running, with a reason that names no GB figures (subagent.md,
    # *macOS: the kernel memory-pressure hold*).

    @staticmethod
    def _level(monkeypatch: pytest.MonkeyPatch, level: int | None) -> None:
        """Fake the kernel's answer under the floor's own reader (the fresh read)."""
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "memory_pressure_level", lambda: level)

    @staticmethod
    def _busy(mgr, **fields: Any) -> None:
        """Give *mgr* one live dedicated child: a runtime of ours the hold can wait on."""
        from kiro_crew.subagent import SubagentInfo

        mgr._agents["busy"] = SubagentInfo(
            id="busy", task="w", parent_session_key="sess-0", _pid=4242, **fields
        )

    @staticmethod
    def _outcomes(mock_sel: MagicMock) -> list[str]:
        return [c[1]["outcome"] for c in mock_sel.return_value.log_tool_invocation.call_args_list]

    @staticmethod
    def _sel_call(mock_sel: MagicMock, outcome: str) -> dict[str, Any]:
        calls = [
            c[1]
            for c in mock_sel.return_value.log_tool_invocation.call_args_list
            if c[1]["outcome"] == outcome
        ]
        assert calls, f"no {outcome!r} SEL row"
        return calls[-1]

    @pytest.mark.parametrize("level", [2, 4])
    def test_pressure_holds_a_start_in_the_capacity_window(
        self, monkeypatch: pytest.MonkeyPatch, level: int
    ) -> None:
        from kiro_crew.subagent_wait_reasons import MEMORY_PRESSURE_DETAIL

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, level)
        info, events, mock_sel = self._spawn_capturing_queued(mgr, memory=(True, 8.0))
        assert info is not None and info.queued is True and info.done is False
        assert info.queued_reason == "memory_pressure"
        assert info.queued_reason_detail == MEMORY_PRESSURE_DETAIL
        assert events[-1]["reason"] == "memory_pressure"
        assert "available_gb" not in events[-1] and "required_gb" not in events[-1]
        assert mgr._queue_wait["sess-1"] == {"reason": "memory_pressure"}
        # A capacity-style wait: the entry is in the window and the store row is
        # queued and due, never deferred.
        assert [p["_preassigned_id"] for p in mgr._queue] == [info.id]
        row = mgr._taskq.get(info.id)
        assert row is not None and row.state == "queued" and row.next_run_at is None
        call = self._sel_call(mock_sel, "deferred_memory_pressure")
        assert call["metadata"]["memory_pressure_level"] == level
        assert call["metadata"]["available_gb"] == pytest.approx(8.0)

    @pytest.mark.parametrize(
        ("busy", "level", "floor_gb", "parent"),
        [
            # Never stuck: nothing of ours to finish, so the start would wait on
            # an episode it cannot end.
            pytest.param(False, 2, 4.0, "sess-1", id="no-runtime-of-ours"),
            pytest.param(True, None, 4.0, "sess-1", id="level-unreadable"),
            pytest.param(True, 1, 4.0, "sess-1", id="level-normal"),
            pytest.param(True, 4, 0.0, "sess-1", id="floor-off"),
            # The busy runtime IS this child's parent, waiting on it: holding the
            # child would hold the parent on an episode only the child can end.
            pytest.param(True, 2, 4.0, "subagent:busy", id="nested-child"),
        ],
    )
    def test_the_hold_lets_a_start_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
        busy: bool,
        level: int | None,
        floor_gb: float,
        parent: str,
    ) -> None:
        mgr = self._mgr()
        if busy:
            self._busy(mgr)
        self._level(monkeypatch, level)
        info, _events, mock_sel = self._spawn_capturing_queued(
            mgr,
            memory=(True, 8.0),
            floor_gb=floor_gb,
            parent_session_key=parent,
        )
        assert info is not None and info.queued_reason != "memory_pressure"
        assert "deferred_memory_pressure" not in self._outcomes(mock_sel)

    @pytest.mark.parametrize("durable", [True, False], ids=["durable", "no-store"])
    def test_a_low_memory_wait_wins_over_the_hold(
        self, monkeypatch: pytest.MonkeyPatch, durable: bool
    ) -> None:
        """Both apply: the user still gets "free up memory" with the figure, and
        the wait carries no pressure clock, so the hold's bound can never end
        it "never started". A start with no row waits in the window, past the
        point where the hold is decided, so it is the case that needs the rule."""
        mgr = self._mgr()
        if not durable:
            mgr._taskq = None
        self._busy(mgr)
        self._level(monkeypatch, 2)
        info, events, mock_sel = self._spawn_capturing_queued(mgr, memory=(False, 3.2))
        assert info is not None and info.queued is True and info.done is False
        assert info.queued_reason == "low_memory"
        assert events[-1]["reason"] == "low_memory"
        assert events[-1]["available_gb"] == pytest.approx(3.2)
        assert "deferred_memory_pressure" not in self._outcomes(mock_sel)
        assert info.id not in mgr._pressure_holds

    def test_a_floor_wait_drops_a_pressure_clock_an_earlier_hold_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Held by the kernel first, then below the floor when re-checked: the
        floor wait carries no pressure clock, so the time it spends there never
        counts toward the hold's bound."""
        mgr = self._mgr()
        mgr._taskq = None
        self._busy(mgr)
        self._level(monkeypatch, 2)
        mgr._pressure_holds["pre-1"] = time.monotonic() - 10_000.0
        mgr._pressure_hold_expired.add("pre-1")
        info, _events, mock_sel = self._spawn_capturing_queued(
            mgr, memory=(False, 3.2), _preassigned_id="pre-1"
        )
        assert info is not None and info.id == "pre-1" and info.queued_reason == "low_memory"
        assert "pre-1" not in mgr._pressure_holds
        assert "pre-1" not in mgr._pressure_hold_expired
        assert "never_started_memory_pressure" not in self._outcomes(mock_sel)

    def test_an_unknown_agent_is_refused_before_the_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.subagent import AGENT_NOT_FOUND_CODE

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        info, _events, mock_sel = self._spawn_capturing_queued(
            mgr, memory=(True, 8.0), agent="no-such-agent-xyz"
        )
        assert info is not None and info.done is True and info.queued is False
        assert info.error_code == AGENT_NOT_FOUND_CODE
        assert "deferred_memory_pressure" not in self._outcomes(mock_sel)

    def test_a_restricted_spawn_waits_in_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No durable row to defer, but a capacity-style wait needs none: a
        temporary spawn waits in the in-memory queue exactly as a capacity-held
        one does, instead of being refused."""
        from kiro_crew.subagent_wait_reasons import MEMORY_PRESSURE_DETAIL

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        info, events, _sel = self._spawn_capturing_queued(
            mgr, memory=(True, 8.0), memory_mode="temporary"
        )
        assert info is not None and info.queued is True and info.done is False
        assert info.queued_reason == "memory_pressure"
        assert info.queued_reason_detail == MEMORY_PRESSURE_DETAIL
        assert mgr._queue_wait["sess-1"] == {"reason": "memory_pressure"}
        assert events[-1]["reason"] == "memory_pressure"
        assert [p["_preassigned_id"] for p in mgr._queue] == [info.id]

    def test_a_held_member_keeps_its_asking_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The hold exits through the capacity queue, which pins the turn that asked."""
        from kiro_crew.crew_log import emit as crew_log_emit

        pinned: list[tuple[str, str, int]] = []
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
        monkeypatch.setattr(
            crew_log_emit,
            "remember_child_origin",
            lambda agent_id, sid, turn: pinned.append((agent_id, sid, turn)),
        )
        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        info, _events, _sel = self._spawn_capturing_queued(
            mgr, memory=(True, 8.0), _crew_log_asked=("sid-1", 7)
        )
        assert info is not None and info.queued_reason == "memory_pressure"
        assert pinned == [(info.id, "sid-1", 7)]

    def test_a_shared_wave_never_holds_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Shared-priced starts are not runtimes the hold waits on, so a wave of
        plain shared spawns at WARN with nothing dedicated running all pass it."""
        from kiro_crew.subagent import _SharingPlan

        mgr = self._mgr()
        mgr._spawn_stagger_secs = 0.0
        monkeypatch.setattr(mgr, "_startup_cap", lambda: 10)
        # Started runs stay live (a run that never returns): each earlier member
        # is a running, shared-priced row when the next one is admitted.
        mgr._is_yolo = lambda: True
        monkeypatch.setattr(mgr, "_run", AsyncMock())
        self._level(monkeypatch, 2)
        plan = MagicMock(return_value=_SharingPlan(eff_model="", eff_effort="", shared=True))
        monkeypatch.setattr(mgr, "_sharing_plan", plan)
        outcomes: list[str] = []
        for _ in range(3):
            info, _events, mock_sel = self._spawn_capturing_queued(mgr, memory=(True, 8.0))
            assert info is not None and info.queued_reason != "memory_pressure"
            outcomes += self._outcomes(mock_sel)
        live = [i for i in mgr._agents.values() if not i.done]
        assert len(live) == 3 and all(i._start_priced_shared for i in live)
        assert plan.call_count == 3
        assert "deferred_memory_pressure" not in outcomes

    def test_a_start_the_floor_prices_shared_is_held_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shared start skips the kiro-cli process but still launches a fresh
        copy of the agent's MCP servers, so it is held while a dedicated one runs."""
        from kiro_crew.subagent import _SharingPlan

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        plan = MagicMock(return_value=_SharingPlan(eff_model="", eff_effort="", shared=True))
        monkeypatch.setattr(mgr, "_sharing_plan", plan)
        info, _events, mock_sel = self._spawn_capturing_queued(mgr, memory=(True, 8.0))
        assert plan.called, "the floor must have priced this start through the sharing plan"
        assert info is not None and info.queued_reason == "memory_pressure"
        assert "deferred_memory_pressure" in self._outcomes(mock_sel)

    def test_a_hold_warns_once_per_level(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every held row is re-checked each pass; a long episode must not log a
        WARNING per row per pass."""
        mgr = self._mgr()
        self._busy(mgr)

        def warnings() -> int:
            return sum(
                r.levelno == logging.WARNING and "macOS reports memory pressure" in r.getMessage()
                for r in caplog.records
            )

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.subagent"):
            self._level(monkeypatch, 2)
            for _ in range(3):
                self._spawn_capturing_queued(mgr, memory=(True, 8.0))
            assert warnings() == 1
            self._level(monkeypatch, 4)
            self._spawn_capturing_queued(mgr, memory=(True, 8.0))
            assert warnings() == 2
            # The episode ends (no reading held), so the next one warns again.
            self._level(monkeypatch, 1)
            self._spawn_capturing_queued(mgr, memory=(True, 8.0))
            self._level(monkeypatch, 4)
            self._spawn_capturing_queued(mgr, memory=(True, 8.0))
            assert warnings() == 3

    def test_a_held_start_expires_once_its_wait_runs_out(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:

        mgr = self._mgr()
        with patch("kiro_crew.subagent.sel") as mock_sel:
            assert mgr._memory_pressure_holds("r1", 2, parent_session_key="sess-1") == "held"
            mgr._pressure_holds["r1"] = time.monotonic() - _HOLD_BOUND_SECS - 1
            with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
                assert mgr._memory_pressure_holds("r1", 2, parent_session_key="sess-1") == "expired"
            # Stays expired, and is said once.
            assert mgr._memory_pressure_holds("r1", 2, parent_session_key="sess-1") == "expired"
        expired = self._sel_call(mock_sel, "never_started_memory_pressure")
        assert expired["metadata"]["subagent_id"] == "r1"
        assert expired["metadata"]["expired_by"] == "wait"
        assert expired["metadata"]["waited_secs"] >= _HOLD_BOUND_SECS
        assert self._outcomes(mock_sel).count("never_started_memory_pressure") == 1
        assert any("for its whole wait" in r.getMessage() for r in caplog.records)

    def test_a_root_start_in_its_lane_restarts_a_held_rows_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bound counts time with no root start in the row's lane (its
        parent session). A row first held longer ago than the bound is still
        waiting its turn while its lane keeps starting rows; another session's
        start is not its progress; once its lane has not started a root for
        the bound, it ends as before."""
        mgr = self._mgr()
        now = time.monotonic()
        with patch("kiro_crew.subagent.sel") as mock_sel:
            mgr._pressure_holds["tail"] = now - 2 * _HOLD_BOUND_SECS
            # Another session's lane is moving; it never extends this row.
            assert mgr._memory_pressure_holds("other-row", 2, parent_session_key="sess-2") == "held"
            mgr._pressure_lane_started("sess-2", now - 5.0)
            assert mgr._memory_pressure_holds(
                "tail", 2, parent_session_key="sess-1", commit_expiry=False
            ) == ("expired")
            mgr._pressure_lane_started("sess-1", now - 5.0)
            assert mgr._memory_pressure_holds("tail", 2, parent_session_key="sess-1") == "held"
            assert "tail" not in mgr._pressure_hold_expired
            # The lane stalls: none of its rows starts for the whole bound.
            mgr._pressure_lane_started_at["sess-1"] = now - _HOLD_BOUND_SECS - 1
            assert mgr._memory_pressure_holds("tail", 2, parent_session_key="sess-1") == "expired"
        expired = self._sel_call(mock_sel, "never_started_memory_pressure")
        assert expired["metadata"]["expired_by"] == "wait"
        # The audit still reports the row's whole wait, from its first hold.
        assert expired["metadata"]["waited_secs"] >= 2 * _HOLD_BOUND_SECS
        # A lane start older than the row's own first hold does not extend it.
        mgr._pressure_holds["late"] = now - _HOLD_BOUND_SECS - 1
        with patch("kiro_crew.subagent.sel"):
            assert mgr._memory_pressure_holds(
                "late", 2, parent_session_key="sess-1", commit_expiry=False
            ) == ("expired")
        mgr._pressure_lane_started_at["sess-1"] = now - 3 * _HOLD_BOUND_SECS
        with patch("kiro_crew.subagent.sel"):
            assert mgr._memory_pressure_holds(
                "late", 2, parent_session_key="sess-1", commit_expiry=False
            ) == ("expired")

    def test_automation_keys_share_one_progress_lane(self) -> None:
        """Every cron, hook, heartbeat or background key, and the empty key, is
        ``SYSTEM_LANE``, one FIFO queue: one automation job's root start is
        progress for every automation row held behind it (documented and
        accepted). A chat's start is still another lane and never extends it."""
        mgr = self._mgr()
        now = time.monotonic()
        with patch("kiro_crew.subagent.sel"):
            mgr._pressure_holds["cron-row"] = now - 2 * _HOLD_BOUND_SECS
            assert mgr._memory_pressure_holds(
                "cron-row", 2, parent_session_key="cron:job-a", commit_expiry=False
            ) == ("expired")
            mgr._pressure_lane_started("sess-1", now - 5.0)
            assert mgr._memory_pressure_holds(
                "cron-row", 2, parent_session_key="cron:job-a", commit_expiry=False
            ) == ("expired")
            # Another automation job's start is a row ahead in the same queue.
            mgr._pressure_lane_started("cron:job-b", now - 5.0)
            assert mgr._memory_pressure_holds(
                "cron-row", 2, parent_session_key="cron:job-a", commit_expiry=False
            ) == ("held")
        assert set(mgr._pressure_hold_lanes.values()) == {"system"}

    def test_a_lane_keeps_no_stamp_once_it_holds_no_row(self) -> None:
        """The progress map has an entry only while its lane holds a row: a
        start in an empty lane stamps nothing, and the lane's last held row
        leaving takes the stamp with it, whichever way it leaves."""
        mgr = self._mgr()
        now = time.monotonic()
        mgr._pressure_lane_started("sess-1", now)
        assert mgr._pressure_lane_started_at == {}
        with patch("kiro_crew.subagent.sel"):
            for agent_id in ("a", "b"):
                assert mgr._memory_pressure_holds(agent_id, 2, parent_session_key="sess-1") == (
                    "held"
                )
        assert mgr._pressure_lane_held == {"sess-1": 2}
        mgr._pressure_lane_started("sess-1", now)
        assert mgr._pressure_lane_started_at == {"sess-1": now}
        mgr._forget_pending_start("a")
        assert mgr._pressure_lane_started_at == {"sess-1": now}
        mgr._forget_pending_start("b")
        assert mgr._pressure_lane_held == {}
        assert mgr._pressure_lane_started_at == {}
        assert mgr._pressure_hold_lanes == {}
        # Forgetting a row twice never drives a lane's count below its rows.
        mgr._forget_pending_start("b")
        assert mgr._pressure_lane_held == {}

    def test_a_young_monotonic_clock_still_expires_a_row_with_no_lane_stamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing lane stamp is its own case, not a 0.0 that would win the
        max: on a host up for less than the bound, the clock itself is smaller
        than the bound and a first hold is negative."""
        from kiro_crew import subagent as subagent_mod

        young = 900.0
        monkeypatch.setattr(
            subagent_mod, "time", SimpleNamespace(monotonic=lambda: young, time=time.time)
        )
        mgr = self._mgr()
        with patch("kiro_crew.subagent.sel"):
            mgr._pressure_holds["solo"] = young - _HOLD_BOUND_SECS - 1
            assert mgr._memory_pressure_holds("solo", 2, commit_expiry=False) == "expired"
            mgr._pressure_holds["member"] = young - _HOLD_BOUND_SECS - 1
            assert (
                mgr._memory_pressure_holds(
                    "member", 2, parent_session_key="sess-1", commit_expiry=False
                )
                == "expired"
            )

    def test_a_held_wave_draining_slower_than_the_bound_never_expires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: every held member of one wave was first held longer ago
        than the bound, and our runtime ends so one member starts. That start
        is its lane's progress, so the members still behind it stay held
        instead of ending "never started" while the queue is moving. The
        episode, here older than the bound too, cannot end them either: a root
        member is only picked on a pass where the hold does not apply, and that
        read ends the episode."""
        mgr, started = self._starting_mgr(monkeypatch, shared=False)
        self._level(monkeypatch, 2)

        async def run() -> tuple[list[Any], float]:
            with self._gate_patches():
                busy = mgr.spawn(task="busy", parent_session_key="sess-0")
                await _wait_for(lambda: busy.id in started, message="the busy run to start")
                held = [
                    mgr.spawn(
                        task=f"t{i}", parent_session_key="sess-1", batch_id="w1", batch_total=3
                    )
                    for i in range(3)
                ]
                assert all(i.queued_reason == "memory_pressure" for i in held)
                stale = time.monotonic() - _HOLD_BOUND_SECS - 1
                for info in held:
                    mgr._pressure_holds[info.id] = stale
                mgr._pressure_episode_since = stale
                assert mgr._memory_pressure_hold() == 2
                assert mgr._pressure_episode_spent is True
                self._end_run(mgr, busy)
                await _wait_for(
                    lambda: sum(i.id in started for i in held) == 1,
                    message="exactly one held member to start",
                )
                # The pump passes again while the started member runs.
                mgr._drain_queue()
            return held, stale

        held, stale = asyncio.run(run())
        waiting = [i for i in held if i.id not in started]
        assert len(waiting) == 2
        for info in waiting:
            assert not info.done and info.error in (None, "")
            assert info.id not in mgr._pressure_hold_expired
        assert mgr._pressure_episode_spent is False
        assert mgr._pressure_lane_started_at["sess-1"] > stale
        assert mgr._pressure_lane_held["sess-1"] == 2
        # The stamp leaves with the lane's last held row.
        for info in waiting:
            mgr._forget_pending_start(info.id)
        assert "sess-1" not in mgr._pressure_lane_started_at
        assert "sess-1" not in mgr._pressure_lane_held

    @pytest.mark.parametrize("trailing_batch", ["w2", ""], ids=["second-wave", "unbatched"])
    def test_a_row_behind_a_draining_wave_in_its_own_lane_is_not_expired(
        self, monkeypatch: pytest.MonkeyPatch, trailing_batch: str
    ) -> None:
        """A row of the same session queued behind a draining wave, a second
        spawn_run wave or an unbatched spawn, waits in the same FIFO lane: the
        wave's starts are its progress too, so it is not ended while the queue
        ahead of it moves."""
        mgr, started = self._starting_mgr(monkeypatch, shared=False)
        self._level(monkeypatch, 2)

        async def run() -> tuple[list[Any], Any]:
            with self._gate_patches():
                busy = mgr.spawn(task="busy", parent_session_key="sess-0")
                await _wait_for(lambda: busy.id in started, message="the busy run to start")
                w1 = [
                    mgr.spawn(
                        task=f"a{i}", parent_session_key="sess-1", batch_id="w1", batch_total=2
                    )
                    for i in range(2)
                ]
                tail = mgr.spawn(
                    task="tail",
                    parent_session_key="sess-1",
                    batch_id=trailing_batch,
                    batch_total=2 if trailing_batch else 0,
                )
                assert all(i.queued_reason == "memory_pressure" for i in [*w1, tail])
                stale = time.monotonic() - _HOLD_BOUND_SECS - 1
                for info in [*w1, tail]:
                    mgr._pressure_holds[info.id] = stale
                mgr._pressure_episode_since = stale
                self._end_run(mgr, busy)
                await _wait_for(
                    lambda: sum(i.id in started for i in w1) == 1,
                    message="the head of the lane to start",
                )
                mgr._drain_queue()
                # flake-ok: pre-existing one-tick yield before the asserts, moved verbatim
                await asyncio.sleep(0)
            return w1, tail

        w1, tail = asyncio.run(run())
        waiting_w1 = [i for i in w1 if i.id not in started]
        assert waiting_w1 and all(i.id not in mgr._pressure_hold_expired for i in waiting_w1)
        rec = mgr._agents.get(tail.id)
        assert tail.id not in mgr._pressure_hold_expired and not (
            rec is not None and rec.done
        ), f"trailing row ended while its lane was draining: {getattr(rec, 'error', None)!r}"

    def test_the_gate_judges_a_held_start_by_its_own_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate's hold check and the pump's pick read the same per-lane
        clock, so the gate passes the start's parent session like the pick
        does, and the row is counted in that lane."""
        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        seen: list[str] = []
        real = mgr._memory_pressure_holds

        def spy(agent_id: str, level: int, **kw: Any) -> str:
            seen.append(kw.get("parent_session_key", "<missing>"))
            return real(agent_id, level, **kw)

        monkeypatch.setattr(mgr, "_memory_pressure_holds", spy)
        with self._gate_patches():
            info = mgr.spawn(task="t", parent_session_key="sess-1", batch_id="w9", batch_total=2)
        assert info.queued_reason == "memory_pressure"
        assert seen and seen[0] == "sess-1"
        assert mgr._pressure_hold_lanes[info.id] == "sess-1"

    def test_a_nested_start_is_not_progress_of_a_held_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nested starts skip the hold, so they cannot show a held lane is
        moving: only a root start stamps its lane."""
        mgr, started = self._starting_mgr(monkeypatch, shared=True)
        # A live parent run that no spawn started here, so nothing stamped yet.
        self._busy(mgr)
        # Both lanes hold a row, so either start could stamp its own lane.
        mgr._pressure_hold_lanes.update({"held-a": "subagent:busy", "held-b": "sess-1"})
        mgr._pressure_lane_held.update({"subagent:busy": 1, "sess-1": 1})

        async def run() -> None:
            with self._gate_patches():
                child = mgr.spawn(
                    task="nested", parent_session_key="subagent:busy", batch_id="w1", batch_total=2
                )
                await _wait_for(lambda: child.id in started, message="the nested run to start")
                assert mgr._pressure_lane_started_at == {}
                root = mgr.spawn(
                    task="root", parent_session_key="sess-1", batch_id="w2", batch_total=2
                )
                await _wait_for(lambda: root.id in started, message="the root run to start")

        asyncio.run(run())
        assert set(mgr._pressure_lane_started_at) == {"sess-1"}

    @pytest.mark.parametrize("child", [False, True])
    def test_releasing_an_approved_root_member_is_its_lanes_progress(
        self, monkeypatch: pytest.MonkeyPatch, child: bool
    ) -> None:
        from kiro_crew.subagent import SubagentInfo

        mgr = self._mgr()
        self._level(monkeypatch, 1)
        if child:
            monkeypatch.setattr(
                type(mgr._admission), "entry_is_child", staticmethod(lambda _p: True)
            )
        with patch("kiro_crew.subagent.sel"):
            assert mgr._memory_pressure_holds("waiter", 1, parent_session_key="sess-1") == "held"

        async def run() -> Any:
            info = SubagentInfo(id="approved", task="t", parent_session_key="sess-1", batch_id="w1")
            info._start_release = asyncio.get_running_loop().create_future()
            mgr._queue.append(
                {
                    "_resume_id": info.id,
                    "_startup_release": True,
                    "_start_info": info,
                    "parent_session_key": info.parent_session_key,
                }
            )
            with self._gate_patches():
                mgr._admission._release_admitted_start_impl()
            return info._start_release

        fut = asyncio.run(run())
        assert fut.result() is True
        assert ("sess-1" in mgr._pressure_lane_started_at) is (not child)

    def test_the_stale_prune_drops_only_rows_that_left(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pause in the hold prunes the clock of a row that left with no
        registration or refusal. It never drops a live row's first-hold clock:
        not one still in the window, nor one whose lane has started a root
        within the window, which can rightly wait longer than it while its
        lane keeps moving. A pruned row leaves its lane too."""
        from kiro_crew.subagent import _PRESSURE_HOLD_PRUNE_FACTOR

        mgr = self._mgr()
        now = time.monotonic()
        old = now - _PRESSURE_HOLD_PRUNE_FACTOR * _HOLD_BOUND_SECS - 10
        with patch("kiro_crew.subagent.sel"):
            for agent_id, lane in (
                ("gone", "sess-gone"),
                ("windowed", "sess-gone"),
                ("moving", "sess-1"),
            ):
                assert mgr._memory_pressure_holds(agent_id, 2, parent_session_key=lane) == "held"
                mgr._pressure_holds[agent_id] = old
        mgr._queue.append({"_preassigned_id": "windowed", "parent_session_key": "sess-gone"})
        mgr._pressure_lane_started("sess-1", now - 5.0)
        mgr._pressure_lane_started(
            "sess-gone", now - _PRESSURE_HOLD_PRUNE_FACTOR * _HOLD_BOUND_SECS - 1
        )
        self._level(monkeypatch, None)
        with self._gate_patches():
            assert mgr._memory_pressure_hold() is None
        assert mgr._pressure_holds == {"windowed": old, "moving": old}
        assert mgr._pressure_lane_held == {"sess-gone": 1, "sess-1": 1}
        assert "gone" not in mgr._pressure_hold_lanes

    def test_a_live_held_rows_clock_survives_a_pause_in_a_progressing_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prune runs on the very passes where the lane makes progress (a
        pause in the hold), so it must keep the first-hold clock of a row whose
        lane is moving: the eventual expiry audit reports the whole wait."""
        from kiro_crew.subagent import _PRESSURE_HOLD_PRUNE_FACTOR

        mgr = self._mgr()
        now = time.monotonic()
        first_hold = now - _PRESSURE_HOLD_PRUNE_FACTOR * _HOLD_BOUND_SECS - 10
        with patch("kiro_crew.subagent.sel"):
            assert mgr._memory_pressure_holds("tail", 2, parent_session_key="sess-1") == "held"
        mgr._pressure_holds["tail"] = first_hold
        mgr._pressure_lane_started("sess-1", now - 5.0)
        with self._gate_patches():
            self._level(monkeypatch, None)
            assert mgr._memory_pressure_hold() is None
            self._busy(mgr)
            self._level(monkeypatch, 2)
            assert mgr._memory_pressure_hold() == 2
            assert (
                mgr._memory_pressure_holds(
                    "tail", 2, parent_session_key="sess-1", commit_expiry=False
                )
                == "held"
            )
        assert mgr._pressure_holds.get("tail") == first_hold

    def test_an_expired_held_row_is_ended_never_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Past its bound a held row does not proceed into the pressure it waited
        on: the next pump pass ends it, never started, its row failed and its
        parent's depth back to 0."""
        from kiro_crew.subagent_wait_reasons import MEMORY_PRESSURE_NEVER_STARTED

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        info, _events, _sel = self._spawn_capturing_queued(mgr, memory=(True, 8.0))
        assert info is not None and info.queued_reason == "memory_pressure"
        mgr._pressure_holds[info.id] = time.monotonic() - _HOLD_BOUND_SECS - 1
        mgr._spawn_stagger_secs = 0.0
        with self._gate_patches():
            mgr._drain_queue()
        row = mgr._taskq.get(info.id)
        assert row is not None and row.state == "failed"
        assert any(
            MEMORY_PRESSURE_NEVER_STARTED in str(e.data) for e in mgr._taskq.events(info.id)
        ), [(e.kind, e.data) for e in mgr._taskq.events(info.id)]
        assert mgr._queue == []
        # Registered as a terminal record: a caller polling the id reads the
        # never-started outcome, not a 404.
        ended = mgr._agents[info.id]
        assert ended.done is True and ended.error == MEMORY_PRESSURE_NEVER_STARTED

    def test_an_approved_start_past_its_bound_is_ended_never_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.subagent import SubagentInfo

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)

        async def run() -> tuple[str, Any]:
            info = SubagentInfo(id="approved", task="t", parent_session_key="sess-1")
            info._start_release = asyncio.get_running_loop().create_future()
            mgr._queue.append(
                {
                    "_resume_id": info.id,
                    "_startup_release": True,
                    "_start_info": info,
                    "parent_session_key": info.parent_session_key,
                }
            )
            mgr._pressure_holds[info.id] = time.monotonic() - _HOLD_BOUND_SECS - 1
            with self._gate_patches():
                outcome = mgr._admission._release_admitted_start_impl()
            return outcome, info._start_release

        outcome, fut = asyncio.run(run())
        assert outcome == "" and fut.result() == "never_started"
        assert mgr._queue == []

    @contextlib.contextmanager
    def _gate_patches(self, *, floor_gb: float = 4.0):
        """The floor reader, the posture verdict, config and SEL, patched for a
        test that drives several spawns and pump passes on one loop."""
        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = floor_gb
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            mock_sel.return_value.log_tool_invocation = MagicMock()
            yield mock_sel

    def _starting_mgr(self, monkeypatch: pytest.MonkeyPatch, *, shared: bool) -> tuple[Any, list]:
        """A manager whose admitted starts register and stay live (a run that
        never returns), priced *shared* or dedicated, with no stagger."""
        from kiro_crew.subagent import _SharingPlan

        mgr = self._mgr()
        mgr._spawn_stagger_secs = 0.0
        monkeypatch.setattr(mgr, "_startup_cap", lambda: 10)
        mgr._is_yolo = lambda: True
        started: list[str] = []

        async def _run(info: Any) -> None:
            started.append(info.id)

        monkeypatch.setattr(mgr, "_run", _run)
        plan = _SharingPlan(eff_model="", eff_effort="", shared=shared)
        monkeypatch.setattr(mgr, "_sharing_plan", lambda *_a, **_k: plan)
        return mgr, started

    def _end_run(self, mgr, info: Any) -> None:
        """The slot release a finished run takes, as ``_run``'s own exit does."""
        info.done = True
        if mgr._release_slot(info):
            mgr._running_count -= 1
            mgr._drain_queue()

    def test_a_chronic_episode_ends_new_starts_at_once_until_it_eases(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A kernel episode that has outlived the bound does not make every new
        start pay the bound in turn, and does not let them into the pressure
        either: each start the hold would keep is ended at once, never started.
        Once the level eases, the next episode holds again."""
        from kiro_crew.subagent_wait_reasons import MEMORY_PRESSURE_NEVER_STARTED

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        with self._gate_patches(), caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
            assert mgr._memory_pressure_hold() == 2
            mgr._pressure_episode_since = time.monotonic() - _HOLD_BOUND_SECS - 1
            assert mgr._memory_pressure_hold() == 2
            assert mgr._memory_pressure_hold() == 2
            said = [r for r in caplog.records if "until it eases" in r.getMessage()]
            assert len(said) == 1
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
            info, _events, sel_mock = self._spawn_capturing_queued(mgr, memory=(True, 8.0))
        assert info is not None and info.done is True and info.queued is False
        assert info.error == MEMORY_PRESSURE_NEVER_STARTED
        # Audited as what happened: the spent episode ended it, it waited nothing.
        expired = self._sel_call(sel_mock, "never_started_memory_pressure")
        assert expired["metadata"]["expired_by"] == "episode"
        assert expired["metadata"]["waited_secs"] < 1
        assert expired["metadata"]["episode_secs"] >= _HOLD_BOUND_SECS
        assert not any("for its whole wait" in r.getMessage() for r in caplog.records)
        assert any("ended without waiting" in r.getMessage() for r in caplog.records)
        with self._gate_patches():
            self._level(monkeypatch, 1)
            assert mgr._memory_pressure_hold() is None
            self._level(monkeypatch, 2)
            assert mgr._memory_pressure_hold() == 2
            assert mgr._memory_pressure_holds("fresh", 2) == "held"

    def test_pressure_with_nothing_of_ours_running_never_spends_an_episode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The episode clock runs only while the hold applies: a Mac at WARN with
        no runtime of ours for longer than the bound must not end the first start
        that meets a runtime of ours with a zero-second wait."""
        mgr = self._mgr()
        self._level(monkeypatch, 2)
        with self._gate_patches():
            assert mgr._memory_pressure_hold() is None
            assert mgr._pressure_episode_since is None
            self._busy(mgr)
            assert mgr._memory_pressure_hold() == 2
            assert mgr._memory_pressure_holds("first", 2) == "held"

    def test_an_unobserved_gap_restarts_the_episode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reads are sampled; a gap longer than a few recheck intervals is a break
        nobody saw, so the next read starts a fresh episode instead of finding a
        spent one and ending a start that never waited."""
        from kiro_crew.subagent import _PRESSURE_EPISODE_MAX_GAP_SECS

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        with self._gate_patches():
            assert mgr._memory_pressure_hold() == 2
            now = time.monotonic()
            mgr._pressure_episode_since = now - _HOLD_BOUND_SECS - 1
            mgr._pressure_episode_read_at = now - _PRESSURE_EPISODE_MAX_GAP_SECS - 1
            assert mgr._memory_pressure_hold() == 2
            assert mgr._pressure_episode_spent is False
            assert mgr._memory_pressure_holds("fresh", 2) == "held"

    def test_the_pick_records_no_never_started_for_a_row_it_only_classifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired row is handed to the gate's re-check; if the level eased in
        between it starts, so the pick must not have audited it as never started."""

        mgr = self._mgr()
        with patch("kiro_crew.subagent.sel") as mock_sel:
            mgr._pressure_holds["r1"] = time.monotonic() - _HOLD_BOUND_SECS - 1
            verdict = mgr._memory_pressure_holds("r1", 2, commit_expiry=False)
        assert verdict == "expired"
        assert "never_started_memory_pressure" not in self._outcomes(mock_sel)
        assert "r1" not in mgr._pressure_hold_expired

    @staticmethod
    def _reload_max_wait(mgr, secs: int) -> None:
        """A live rewrite of ``agent.subagent_queue_max_wait_secs``, adopted the
        way the config watcher adopts it (``apply_limits``), with no restart."""
        cfg = KiroCrewConfig()
        cfg.agent.subagent_queue_max_wait_secs = secs
        mgr.apply_limits(cfg, max_concurrent=3)
        assert mgr._subagent_queue_max_wait_secs == secs

    def test_the_hold_is_bounded_by_the_live_queue_max_wait_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-start bound is ``agent.subagent_queue_max_wait_secs``, read at
        each check: a start held 61 s is still waiting under the default, ends
        once the key is reloaded to 60, and 0 lifts the bound again."""
        mgr = self._mgr()
        assert mgr._subagent_queue_max_wait_secs == DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS
        with patch("kiro_crew.subagent.sel"):
            mgr._pressure_holds["r1"] = time.monotonic() - 61
            assert mgr._memory_pressure_holds("r1", 2, commit_expiry=False) == "held"
            self._reload_max_wait(mgr, 60)
            assert mgr._memory_pressure_holds("r1", 2) == "expired"
            self._reload_max_wait(mgr, 0)
            mgr._pressure_holds["r2"] = time.monotonic() - 10 * _HOLD_BOUND_SECS
            assert mgr._memory_pressure_holds("r2", 2) == "held"

    def test_a_live_bound_spends_and_unspends_the_episode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The episode bound is the same live key: an episode 61 s old is spent
        once the key is 60, and a reload to 0 (no bound) or past the episode's
        length un-spends it, so new starts are held again rather than ended."""
        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        with self._gate_patches():
            assert mgr._memory_pressure_hold() == 2
            mgr._pressure_episode_since = time.monotonic() - 61
            assert mgr._memory_pressure_hold() == 2
            assert mgr._pressure_episode_spent is False
            self._reload_max_wait(mgr, 60)
            assert mgr._memory_pressure_hold() == 2
            assert mgr._pressure_episode_spent is True
            assert mgr._memory_pressure_holds("ended", 2) == "expired"
            self._reload_max_wait(mgr, 0)
            assert mgr._memory_pressure_hold() == 2
            assert mgr._pressure_episode_spent is False
            assert mgr._memory_pressure_holds("fresh", 2) == "held"
            self._reload_max_wait(mgr, 60)
            assert mgr._memory_pressure_hold() == 2
            assert mgr._pressure_episode_spent is True
            self._reload_max_wait(mgr, 3600)
            assert mgr._memory_pressure_hold() == 2
            assert mgr._pressure_episode_spent is False

    def test_a_raised_bound_keeps_a_paused_rows_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A row's clock survives a pause in the hold for a few bounds, and the
        bound is the live key: raised to 20000 s, a clock 10000 s old is kept
        across the pause (it would be dropped under the default), while one
        older than four bounds is still dropped."""
        mgr = self._mgr()
        self._reload_max_wait(mgr, 20000)
        now = time.monotonic()
        mgr._pressure_holds["paused"] = now - 10000
        mgr._pressure_holds["gone"] = now - 4 * 20000 - 1
        self._level(monkeypatch, 1)
        with self._gate_patches():
            assert mgr._memory_pressure_hold() is None
        assert "paused" in mgr._pressure_holds
        assert "gone" not in mgr._pressure_holds

    def test_the_held_wave_starts_as_soon_as_our_runtime_ends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The slot release pumps the window and the pick lets the held roots go,
        with no recheck timer and no admit wait in between; while our runtime
        runs, the pick passes over them without even re-entering the gate."""
        from kiro_crew.subagent import _SharingPlan

        mgr, started = self._starting_mgr(monkeypatch, shared=True)
        self._level(monkeypatch, 2)
        dedicated = _SharingPlan(eff_model="", eff_effort="", shared=False)

        async def run() -> tuple[list[Any], MagicMock]:
            with self._gate_patches():
                # Our runtime: a dedicated start, the one thing that holds others.
                with patch.object(mgr, "_sharing_plan", lambda *_a, **_k: dedicated):
                    busy = mgr.spawn(task="busy", parent_session_key="sess-0")
                await _wait_for(lambda: busy.id in started, message="the busy run to start")
                held = [mgr.spawn(task=f"t{i}", parent_session_key="sess-1") for i in range(3)]
                assert all(i.queued_reason == "memory_pressure" for i in held)
                reentries = MagicMock(wraps=mgr.spawn)
                with patch.object(mgr, "spawn", reentries):
                    mgr._drain_queue()
                reentries.assert_not_called()
                self._end_run(mgr, busy)
                await _wait_for(
                    lambda: all(i.id in started for i in held),
                    message="every held member to start once our runtime ended",
                )
            return held, reentries

        held, _reentries = asyncio.run(run())
        assert mgr._queue == []
        assert all(not mgr._agents[i.id].done for i in held)

    def test_a_dedicated_wave_is_released_one_runtime_at_a_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A released member that is itself dedicated is a runtime of ours, so the
        rest wait on it; their first-hold clocks keep running across the pause
        (the bound counts from the later of that clock and its lane's last root
        start)."""
        mgr, started = self._starting_mgr(monkeypatch, shared=False)
        self._level(monkeypatch, 2)

        async def run() -> tuple[Any, list[Any], dict[str, float]]:
            with self._gate_patches():
                busy = mgr.spawn(task="busy", parent_session_key="sess-0")
                await _wait_for(lambda: busy.id in started, message="the busy run to start")
                held = [mgr.spawn(task=f"t{i}", parent_session_key="sess-1") for i in range(3)]
                clocks = dict(mgr._pressure_holds)
                self._end_run(mgr, busy)
                await _wait_for(
                    lambda: sum(i.id in started for i in held) == 1,
                    message="exactly one held member to start",
                )
            return busy, held, clocks

        _busy, held, clocks = asyncio.run(run())
        waiting = [i for i in held if i.id not in started]
        assert len(waiting) == 2
        for info in waiting:
            assert mgr._pressure_holds[info.id] == clocks[info.id]

    def test_the_recheck_timer_notices_pressure_easing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing finishes, the level drops: only the recheck timer can notice."""
        from kiro_crew import subagent_wait_reasons

        monkeypatch.setattr(subagent_wait_reasons, "MEMORY_PRESSURE_RECHECK_SECS", 0.05)
        monkeypatch.setattr("kiro_crew.subagent.MEMORY_PRESSURE_RECHECK_SECS", 0.05)
        mgr, started = self._starting_mgr(monkeypatch, shared=True)
        self._busy(mgr)
        self._level(monkeypatch, 2)

        async def run() -> Any:
            with self._gate_patches():
                info = mgr.spawn(task="t", parent_session_key="sess-1")
                assert info.queued_reason == "memory_pressure"
                assert mgr._pressure_recheck_handle is not None
                self._level(monkeypatch, 1)
                await _wait_for(lambda: info.id in started, message="the timer's re-pump")
            return info

        asyncio.run(run())
        assert mgr._pressure_recheck_handle is None

    def test_the_recheck_timer_is_cancelled_at_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)

        async def run() -> Any:
            with self._gate_patches():
                assert mgr._memory_pressure_hold() == 2
                handle = mgr._pressure_recheck_handle
                assert handle is not None
                mgr._agents.clear()
                await mgr.cancel_all()
            return handle

        handle = asyncio.run(run())
        assert handle.cancelled() and mgr._pressure_recheck_handle is None

    def test_a_row_first_held_at_the_pick_is_relabelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Queued for capacity before the pressure began, then held at the pick:
        its parent's label must say why it waits now, and the pump read no
        figure, so the audit carries none."""
        mgr, started = self._starting_mgr(monkeypatch, shared=True)
        self._level(monkeypatch, None)
        events, on_event = self._queued_sink()
        mgr._on_event = on_event

        async def run() -> tuple[Any, MagicMock]:
            with self._gate_patches() as mock_sel:
                mgr._max_concurrent = 1
                first = mgr.spawn(task="a", parent_session_key="sess-0")
                waiting = mgr.spawn(task="b", parent_session_key="sess-1")
                assert waiting.queued_reason == "concurrency_limit"
                # The running one is now dedicated, and the kernel says WARN.
                mgr._agents[first.id]._start_priced_shared = False
                self._level(monkeypatch, 2)
                mgr._max_concurrent = 3
                mgr._drain_queue()
                await _await_queued_event(events, reason="memory_pressure")
            return waiting, mock_sel

        waiting, mock_sel = asyncio.run(run())
        assert waiting.id not in started
        assert mgr._queue_wait["sess-1"] == {"reason": "memory_pressure"}
        metadata = self._sel_call(mock_sel, "deferred_memory_pressure")["metadata"]
        assert "available_gb" not in metadata

    def test_the_label_returns_to_capacity_when_the_hold_ends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        info, _events, _sel = self._spawn_capturing_queued(mgr, memory=(True, 8.0))
        assert mgr._queue_wait["sess-1"] == {"reason": "memory_pressure"}
        self._level(monkeypatch, 1)
        with self._gate_patches():
            assert mgr._memory_pressure_hold() is None
        assert mgr._queue_wait["sess-1"] == {"reason": "concurrency_limit"}
        # A pause is not an end of the row's wait: its clock is kept.
        assert info.id in mgr._pressure_holds

    def test_the_hold_names_itself_over_a_full_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The binding reason wins the label: with the cap full the start would
        queue anyway, but it would not start on the next slot either."""
        mgr = self._mgr()
        self._busy(mgr)
        mgr._running_count = mgr._max_concurrent
        self._level(monkeypatch, 2)
        info, _events, _sel = self._spawn_capturing_queued(mgr, memory=(True, 8.0))
        assert info is not None and info.queued_reason == "memory_pressure"

    def test_a_zero_cap_has_no_pause_label_under_pressure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no pause kind: the adaptive controller never takes the
        execution cap to 0 (it reads no memory or loop lag). A cap a caller
        pinned to 0 is an ordinary capacity wait, so the kernel's pressure
        verdict labels the row, and the end of the hold relabels to the bare
        capacity kind."""
        mgr = self._mgr()
        self._busy(mgr)
        mgr._max_concurrent = 0
        self._level(monkeypatch, 2)
        info, _events, _sel = self._spawn_capturing_queued(mgr, memory=(True, 8.0))
        assert info is not None and info.queued_reason == "memory_pressure"
        mgr._queue_wait["sess-1"] = {"reason": "memory_pressure"}
        mgr._pressure_hold_on = True
        self._level(monkeypatch, 1)
        with self._gate_patches():
            assert mgr._memory_pressure_hold() is None
        assert mgr._queue_wait["sess-1"] == {"reason": "concurrency_limit"}

    def test_a_held_root_release_does_not_block_a_child_behind_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child exemption must hold at release too: a pressure-held root at
        the head of the released starts is passed over, not waited on."""
        from kiro_crew.subagent import SubagentInfo

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)

        async def run() -> tuple[list[str], bool, bool]:
            loop = asyncio.get_running_loop()
            root = SubagentInfo(id="root", task="t", parent_session_key="sess-1")
            child = SubagentInfo(id="child", task="t", parent_session_key="subagent:busy")
            for info in (root, child):
                info._start_release = loop.create_future()
                mgr._queue.append(
                    {
                        "_resume_id": info.id,
                        "_startup_release": True,
                        "_start_info": info,
                        "parent_session_key": info.parent_session_key,
                    }
                )
            with self._gate_patches():
                outcomes = [mgr._admission._release_admitted_start_impl()]
                mgr._last_spawn_ts = 0.0
                outcomes.append(mgr._admission._release_admitted_start_impl())
            return outcomes, child._start_release.done(), root._start_release.done()

        outcomes, child_released, root_released = asyncio.run(run())
        assert outcomes == ["released", "held"]
        assert child_released is True and root_released is False

    def test_a_stopped_release_behind_a_held_root_is_retired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An approved start stopped while it waited, queued behind a held root,
        is popped and its waiter woken now, not when the root's hold ends."""
        from kiro_crew.subagent import SubagentInfo

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)

        async def run() -> tuple[str, Any, Any]:
            loop = asyncio.get_running_loop()
            root = SubagentInfo(id="root", task="t", parent_session_key="sess-1")
            stopped = SubagentInfo(id="stopped", task="t", parent_session_key="sess-2")
            for info in (root, stopped):
                info._start_release = loop.create_future()
                mgr._queue.append(
                    {
                        "_resume_id": info.id,
                        "_startup_release": True,
                        "_start_info": info,
                        "parent_session_key": info.parent_session_key,
                    }
                )
            stopped.user_stopped = True
            with self._gate_patches():
                outcome = mgr._admission._release_admitted_start_impl()
            return outcome, root._start_release, stopped._start_release

        outcome, root_fut, stopped_fut = asyncio.run(run())
        assert outcome == "held"
        assert stopped_fut.done() and stopped_fut.result() is False
        assert not root_fut.done()
        assert [p["_resume_id"] for p in mgr._queue] == ["root"]

    def test_an_approved_start_is_held_at_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A prompt answered after the hold began must not launch past it."""
        from kiro_crew.subagent import SubagentInfo

        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)

        async def run() -> tuple[str, bool, str]:
            info = SubagentInfo(id="approved", task="t", parent_session_key="sess-1")
            fut = asyncio.get_running_loop().create_future()
            info._start_release = fut
            mgr._queue.append(
                {
                    "_resume_id": info.id,
                    "_startup_release": True,
                    "_start_info": info,
                    "parent_session_key": info.parent_session_key,
                }
            )
            with patch("kiro_crew.subagent.sel"):
                held = mgr._admission._release_admitted_start_impl()
                waiting = not fut.done()
                self._level(monkeypatch, 1)
                released = mgr._admission._release_admitted_start_impl()
            return held, waiting, released

        held, waiting, released = asyncio.run(run())
        assert (held, waiting, released) == ("held", True, "released")

    def test_a_held_app_spawn_keeps_its_auto_approval_across_a_refill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The store never carries ``approval_mode``; a window refill restores it
        from this process's side table, so the drained App Kit spawn raises no
        prompt nobody can answer."""
        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        info, _events, _sel = self._spawn_capturing_queued(
            mgr, memory=(True, 8.0), approval_mode="auto"
        )
        assert info is not None and info.queued_reason == "memory_pressure"
        mgr._queue.clear()  # spilled to store-only, as a full window does
        mgr._admission.taskq_refill_window()
        entries = [p for p in mgr._queue if p.get("_preassigned_id") == info.id]
        assert len(entries) == 1 and entries[0]["approval_mode"] == "auto"
        mgr._agents["busy"].done = True
        prompts = AsyncMock(return_value=True)
        mgr._on_spawn_approval = prompts
        monkeypatch.setattr(mgr, "_run", AsyncMock())
        params = {k: v for k, v in entries[0].items() if k != "_lane"}

        async def drain() -> Any:
            with (
                patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
                patch("kiro_crew.subagent.sel") as mock_sel,
            ):
                started = mgr.spawn(**params, _from_queue=True)
                # flake-ok: pre-existing one-tick yield before the asserts, moved verbatim
                await asyncio.sleep(0)
            return started, mock_sel

        started, mock_sel = asyncio.run(drain())
        assert started is not None and started.approval_mode == "auto"
        assert self._sel_call(mock_sel, "auto_approved_spawn")["metadata"]["reason"] == (
            "approval_mode_auto"
        )
        prompts.assert_not_called()
        assert info.id not in mgr._held_approval_modes

    def test_an_unreadable_macos_figure_is_reported_like_linux(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor fails open on an unmeasurable host, but never silently: the
        macOS twin of Linux's ``memory_check_unavailable``. The hold still applies,
        and its audit records the figure as unknown, not as -1."""
        import kiro_crew.subagent as subagent_mod
        from kiro_crew import platform_compat

        for name in ("LINUX", "WINDOWS", "MACOS"):
            monkeypatch.setattr(platform_compat, "IS_" + name, name == "MACOS")
        monkeypatch.setattr(subagent_mod, "_host_available_gb", lambda: -1.0)
        mgr = self._mgr()
        self._busy(mgr)
        self._level(monkeypatch, 2)
        info, _events, mock_sel = self._spawn_capturing_queued(mgr)
        assert info is not None and info.queued_reason == "memory_pressure"
        assert "memory_check_unavailable" in self._outcomes(mock_sel)
        assert (
            self._sel_call(mock_sel, "deferred_memory_pressure")["metadata"]["available_gb"] is None
        )

    def test_the_real_reader_holds_a_start_through_the_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end on the readers: only the platform flags, the Mach figure and
        the kernel level are faked. The floor check runs the real
        ``check_memory_available``, and the level is read fresh through the real
        ``read_memory_pressure_level``."""
        import kiro_crew.subagent as subagent_mod
        from kiro_crew import platform_compat

        for name in ("LINUX", "WINDOWS", "MACOS"):
            monkeypatch.setattr(platform_compat, "IS_" + name, name == "MACOS")
        # 8 GB reclaimable clears the 4.0 GB floor plus warming-start reserves.
        monkeypatch.setattr(subagent_mod, "_host_available_gb", lambda: 8.0)
        self._level(monkeypatch, platform_compat.MEMORY_PRESSURE_WARN)
        mgr = self._mgr()
        self._busy(mgr)
        info, events, _sel = self._spawn_capturing_queued(mgr)
        assert info is not None and info.queued is True
        assert info.queued_reason == "memory_pressure"
        assert events[-1]["reason"] == "memory_pressure"

    def test_the_pump_keeps_a_held_row_in_the_window_without_a_defer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On the coroutine pump the held row is passed over by the pick: no store
        defer is written, the label stays the pressure reason, and the row stays
        queued and due for the next pass."""
        from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

        monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
        monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
        self._level(monkeypatch, 2)
        events, on_event = self._queued_sink()

        async def run() -> tuple[Any, Any, MagicMock]:
            mgr = self._mgr()
            await wait_taskq_open(mgr)
            self._busy(mgr)
            mgr._spawn_stagger_secs = 0.0
            mgr._on_event = on_event
            park = MagicMock(wraps=mgr._admission.park_defer)
            with (
                patch("kiro_crew.subagent.check_memory_available", return_value=(True, 8.0)),
                patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
                patch("kiro_crew.subagent.sel"),
                patch.object(type(mgr._admission), "park_defer", park),
            ):
                mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
                mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
                info = await mgr.spawn_async("test task", parent_session_key="sess-1")
                await _await_queued_event(events, reason="memory_pressure")
                for _ in range(3):
                    mgr._drain_queue()
                    await _wait_for(
                        lambda: mgr._drain_task is None or mgr._drain_task.done(),
                        message="the pump pass to finish",
                    )
            return info, mgr, park

        info, mgr, park = asyncio.run(run())
        assert info is not None and info.queued_reason == "memory_pressure"
        park.assert_not_called()
        row = mgr._taskq.get(info.id)
        assert row is not None and row.state == "queued" and row.next_run_at is None
        assert mgr._queue_wait["sess-1"] == {"reason": "memory_pressure"}

    def test_parked_defer_publishes_the_label_only_after_the_write_succeeds(self) -> None:
        """The coroutine dispatcher writes the defer off the loop, after the gate
        returned. The label must ride on THAT emit: published earlier, a row the
        store turned out not to hold (refused, not queued) would leave a memory
        label on the parent for its other, capacity-queued rows to wear."""
        from kiro_crew.subagent import SubagentInfo

        mgr = self._mgr()
        store = mgr._taskq
        assert store is not None
        events: list[dict[str, Any]] = []

        async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
            if etype == "subagent_queued":
                events.append(dict(extra))

        mgr._on_event = on_event
        wait = {"reason": "low_memory", "available_gb": 3.2, "required_gb": 4.5}

        def _park(agent_id: str) -> SubagentInfo:
            queued = SubagentInfo(id=agent_id, task="t", parent_session_key="sess-1", queued=True)
            refused = SubagentInfo(
                id=agent_id, task="t", parent_session_key="sess-1", done=True, error="refused"
            )
            mgr._admission.park_defer(
                agent_id,
                reason="low memory: 3.2 GB available, need 4 GB",
                parent_session_key="sess-1",
                batch_id="",
                queued=queued,
                refused=refused,
                wait=wait,
            )
            return queued

        async def run() -> tuple[Any, Any]:
            with patch.object(type(mgr), "_announce_rejection", lambda self, info: info):
                # No row behind this id: the write reports none and the row is
                # refused -- no label may be left behind.
                missing = await mgr._admission.finish_parked_defer(_park("ghost"))
                no_label_after_refusal = dict(mgr._queue_wait)
                # A real row: the write succeeds and the label rides the emit.
                rec = mgr._admission.taskq_build_record(
                    "row1",
                    {"task": "t", "parent_session_key": "sess-1"},
                    parent_session_key="sess-1",
                    memory_store="",
                    app="",
                    model="",
                    allowed_tools=None,
                    approval_mode=None,
                )
                store.accept([rec])
                held = await mgr._admission.finish_parked_defer(_park("row1"))
            await _await_queued_event(events)
            return (missing, no_label_after_refusal), held

        (missing, no_label_after_refusal), held = asyncio.run(run())
        assert missing.done is True and missing.error == "refused"
        assert no_label_after_refusal == {}
        assert held.queued is True and held.done is False
        assert mgr._queue_wait.get("sess-1", {}).get("reason") == "low_memory"
        assert events and events[-1]["reason"] == "low_memory"
