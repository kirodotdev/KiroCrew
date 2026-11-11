"""The dispatcher stays off the gateway boot path.

Split from ``test_restart_resume.py`` (the 2,000-line cap); the boot-path tests
are self-contained and read the dispatcher only through its public module.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from test_restart_resume import (  # noqa: F401 -- the fixture is requested by name
    _cfg,
    _cut_off,
    _mock_state,
    _slot,
    quiet_dispatch_fixture,
)
from test_restart_resume_budget import _FakeLog

from kiro_crew.dashboard import restart_resume
from kiro_crew.dashboard.chat_utils import _AUTO_RESUME_RESTART_MSG
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until


class TestTheDeferredReading:
    """The restart marker is read twice: once from the metadata line as the slot
    is built, and again (``AppliedMeta.settle``) once the window is loaded and
    the marker reconciled. The window load awaits a worker thread, so a person's
    Stop can land between the two readings; ``cancel_armed_retry`` clears the
    flag and the second reading must not put it back (GPT review, rev 48)."""

    def test_settle_keeps_a_stop_that_landed_during_the_window_load(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("cut")
        meta = {"_type": "metadata", "turn_in_flight_generation": 3}
        applied = codec.apply(state, slot, meta, codec.Restore(name="cut", member=None))
        assert slot._restart_cut_turn is True  # the first reading
        slot.append("user", "do the thing", "msg msg-u")
        slot._disk_window_len = len(slot.messages)

        restart_resume.cancel_armed_retry(slot)  # a Stop while the window loads
        had_marker = applied.settle(list(slot.messages))

        assert had_marker is True  # the marker was there; the person still won
        assert slot._restart_cut_turn is False

    def test_settle_writes_the_marker_when_nothing_cleared_it(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("cut")
        applied = codec.apply(
            state,
            slot,
            {"_type": "metadata", "turn_in_flight_generation": 3},
            codec.Restore(name="cut", member=None),
        )
        slot.append("user", "do the thing", "msg msg-u")
        slot._disk_window_len = len(slot.messages)

        assert applied.settle(list(slot.messages)) is True
        assert slot._restart_cut_turn is True


class TestBootPath:
    """When the restore schedules the dispatcher, and that it does so once.

    The restore schedules the boot pass on every boot, switch on or off: a planned
    restart's record resumes its chats with the switch off, and the module
    is on the startup path anyway -- ``handlers.updates_apply`` imports it at module
    load, before an update apply can replace the tree -- so gating the import behind
    the switch (rev 76 and earlier) bought nothing. What a default-off boot pays is
    one ``take_intent`` file read; the codec's budget-list validation lives in
    ``auto_resume_stamps`` and runs without calling into the dispatcher."""

    @pytest.mark.asyncio
    async def test_a_default_off_restore_still_schedules_the_boot_pass(self, tmp_path, monkeypatch):
        """The pass decides what the switch allows; a planned restart's record resumes
        with it off, so the restore arms the cut chats and schedules the pass either way."""
        state, cfg, calls = await self._restore(tmp_path, monkeypatch, enabled=False)

        assert calls == [("armed", state), (state, cfg)]

    @pytest.mark.asyncio
    async def test_a_restore_with_the_switch_on_schedules_the_boot_pass(
        self, tmp_path, monkeypatch
    ):
        state, cfg, calls = await self._restore(tmp_path, monkeypatch, enabled=True)

        assert calls == [("armed", state), (state, cfg)]

    def test_the_dashboard_boot_schedules_no_second_resume(self):
        """One boot pass, from the restore: ``start_dashboard`` schedules none of its own;
        the planned-restart record is consumed by the pass."""
        import ast
        import inspect

        from kiro_crew.dashboard import server

        tree = ast.parse(inspect.getsource(server.start_dashboard))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "schedule_resume_after_restart" not in names
        assert not hasattr(restart_resume, "schedule_resume_after_restart")

    @pytest.mark.asyncio
    async def test_the_restore_arms_the_cut_chats_before_its_next_await(
        self, tmp_path, monkeypatch
    ):
        """A channel Stop can land while the restore still awaits (the recent-session
        restore follows the open-tab one); a cut chat's key is armed the moment the
        open tabs exist, so that Stop is recorded (GPT review, rev 50)."""
        order: list[str] = []

        await self._restore(tmp_path, monkeypatch, enabled=True, order=order)

        assert order == ["open", "armed", "recent", "boot"]

    @staticmethod
    async def _restore(tmp_path, monkeypatch, *, enabled: bool, order: list[str] | None = None):
        # The restore body with its own collaborators stubbed: no sessions to
        # rebuild, no cautious-boot pause, no sweeps. What is left is the gate.
        from kiro_crew.dashboard import server
        from kiro_crew.dashboard.server_runtime import session_restore

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        cfg = SimpleNamespace(
            dashboard=SimpleNamespace(
                resume_interrupted_turns=enabled,
                restore_sessions=False,
                restore_window_minutes=0,
                idle_slot_sweep_days=0,
                surface_channel_sessions=False,
            )
        )
        seen = order if order is not None else []
        monkeypatch.setattr(server.cautious_boot, "pause_before", AsyncMock())

        async def open_slots(st):
            seen.append("open")

        async def recent(st, *a, **k):
            seen.append("recent")
            return 0

        monkeypatch.setattr(server.chat, "restore_open_slots_async", open_slots)
        monkeypatch.setattr(server.chat, "restore_recent_sessions_async", recent)
        calls: list[tuple] = []

        def arm(st):
            seen.append("armed")
            calls.append(("armed", st))
            return 0

        def boot_pass(st, c):
            seen.append("boot")
            calls.append((st, c))
            return MagicMock()

        monkeypatch.setattr(restart_resume, "arm_cut_chats", arm, raising=False)
        monkeypatch.setattr(restart_resume, "start_boot_pass", boot_pass)

        await session_restore._restore_dashboard_sessions(state, cfg)
        return state, cfg, calls


# ── The planned-restart record ──────────────────────────────────────────────────


def _record(monkeypatch, tmp_path, names: list[str] | None):
    """Point the record at *tmp_path*; write *names* as a planned restart would."""
    path = tmp_path / "resume-intent.json"
    monkeypatch.setattr(restart_resume, "intent_path", lambda: path)
    if names is not None:
        restart_resume.write_intent(names)
    return path


class TestPlannedRestart:
    """A planned restart's chats resume with the switch off, once, within the budget
    and under the Resume-all bound; a crash writes no record and waits for the switch."""

    @pytest.fixture(autouse=True)
    def _no_memory_wait(self, monkeypatch):
        settled = AsyncMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_resume_all._memory_preparation_settled", settled
        )
        return settled

    @pytest.mark.asyncio
    async def test_the_recorded_chats_resume_with_the_switch_off_once(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        cut = _cut_off(_slot("cut"))
        crashed_earlier = _cut_off(_slot("crashed-earlier"))
        state = _mock_state(cut, crashed_earlier)
        path = _record(monkeypatch, tmp_path, ["cut"])

        dispatched = await restart_resume.resume_after_restart(state, _cfg(enabled=False))

        assert dispatched == 1
        assert [c["slot"] for c in quiet_dispatch] == ["cut"]
        assert quiet_dispatch[0]["via"] == restart_resume.VIA_RESTART
        assert quiet_dispatch[0]["body"] == _AUTO_RESUME_RESTART_MSG
        assert not path.exists(), "the record is cleared after its one attempt"
        # The chat the record did not name is a crash's: left for the person, and
        # this boot's reading is spent for it too.
        assert crashed_earlier._restart_cut_turn is False
        assert len(cut._auto_resume_stamps) == 1, "a planned restart's resume counts"
        # The next boot finds no record and resumes nothing, so it never loops.
        assert await restart_resume.resume_after_restart(state, _cfg(enabled=False)) == 0
        assert len(quiet_dispatch) == 1

    @staticmethod
    def _real_state(tmp_path, monkeypatch):
        """A real ``DashboardState`` (modes, lineage and the under-construction set are
        read by ``collect_interrupted_idle``; a ``MagicMock`` answers them by accident)."""
        from kiro_crew import crew_mode

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(crew_mode, "_known_roots", set())
        monkeypatch.setattr(crew_mode, "persisted_parents", dict)
        monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
        state = _make_state(tmp_path)
        state._background_tasks = set()
        state.notify = MagicMock()
        state.conversation_log = _FakeLog(True)  # the budget write succeeds: nothing else refuses
        return state

    @pytest.mark.asyncio
    async def test_with_the_switch_off_a_planned_restart_resumes_only_what_resume_all_would(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        """The record names every slot that carried a turn; the switch-off resume is
        narrower, and exactly ``main``'s: the Resume-all candidate set (the planned-restart
        run was ``start_resume_all(only=names, crew_mode=True)``). A mate's DM thread lives on
        another page, a Crew Mode worker whose root is listed is its conductor's, so
        neither resumes on a default-off gateway -- their turns would otherwise run
        under the mode the profile re-seeds, with nobody there. The plain chats the
        record names do resume."""
        from kiro_crew.members import DM_SLOT_MODE

        state = self._real_state(tmp_path, monkeypatch)
        _cut_off(state.get_or_create_slot("plain"))
        _cut_off(state.get_or_create_slot("member-mate", agent="mate", mode=DM_SLOT_MODE))
        root = state.get_or_create_slot("conductor")
        root.agent = "kirocrew-conductor"
        _cut_off(root)
        worker = state.get_or_create_slot("worker")
        worker._created_by = "conductor"
        _cut_off(worker)
        _record(monkeypatch, tmp_path, ["plain", "member-mate", "conductor", "worker"])

        dispatched = await restart_resume.resume_after_restart(state, _cfg(enabled=False))

        assert dispatched == 2
        assert sorted(c["slot"] for c in quiet_dispatch) == ["conductor", "plain"]
        # The thread and the worker are left exactly as the restart left them: no
        # stamp spent, and this boot's reading spent so no later pass re-reads them.
        for key in ("member-mate", "worker"):
            slot = state._slots[key]
            assert slot._auto_resume_stamps == []
            assert slot._restart_cut_turn is False

    @pytest.mark.asyncio
    async def test_the_filter_is_resume_alls_own(self, tmp_path, monkeypatch, quiet_dispatch):
        """The switch-off set is computed by ``collect_interrupted_idle`` itself, not by
        a copy of its rules, so the two cannot drift: narrow it and the pass narrows."""
        from kiro_crew.dashboard import chat_resume_all

        state = self._real_state(tmp_path, monkeypatch)
        _cut_off(state.get_or_create_slot("a"))
        _cut_off(state.get_or_create_slot("b"))
        _record(monkeypatch, tmp_path, ["a", "b"])
        seen: list[bool] = []
        original = chat_resume_all.collect_interrupted_idle

        def only_a(st, *, crew_mode=False):
            seen.append(crew_mode)
            return [(n, s) for n, s in original(st, crew_mode=crew_mode) if n == "a"]

        monkeypatch.setattr(chat_resume_all, "collect_interrupted_idle", only_a)

        assert await restart_resume.resume_after_restart(state, _cfg(enabled=False)) == 1
        assert [c["slot"] for c in quiet_dispatch] == ["a"]
        assert seen == [True], "asked once, with crew_mode=True as the planned-restart run did"

    @pytest.mark.asyncio
    async def test_with_the_switch_on_the_pass_keeps_its_own_eligibility(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        """The Resume-all filter bounds the switch-OFF resume only. With the switch on, a
        recorded mate thread is the pass's to resume, as the RFC decides (§4.6, §9 Q7:
        its turn runs under the mode its profile declares), whether or not a record
        names it."""
        from kiro_crew.members import DM_SLOT_MODE

        state = self._real_state(tmp_path, monkeypatch)
        _cut_off(state.get_or_create_slot("member-mate", agent="mate", mode=DM_SLOT_MODE))
        _record(monkeypatch, tmp_path, ["member-mate"])

        assert await restart_resume.resume_after_restart(state, _cfg(enabled=True)) == 1
        assert [c["slot"] for c in quiet_dispatch] == ["member-mate"]

    @pytest.mark.asyncio
    async def test_a_record_left_by_a_restart_that_never_happened_resumes_nothing(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        """A record older than a restart takes belongs to a restart that did not run:
        a later crash boot must not resume those chats, and the record is still cleared."""
        import os
        import time

        state = _mock_state(_cut_off(_slot("cut")))
        path = _record(monkeypatch, tmp_path, ["cut"])
        old = time.time() - restart_resume._INTENT_MAX_AGE_SECS - 60
        os.utime(path, (old, old))

        assert await restart_resume.resume_after_restart(state, _cfg(enabled=False)) == 0
        assert quiet_dispatch == []
        assert not path.exists()

    def test_a_planned_restart_with_no_running_chat_clears_an_earlier_record(
        self, tmp_path, monkeypatch
    ):
        """An empty record removes a stale one, so it cannot outlive this restart."""
        path = _record(monkeypatch, tmp_path, ["left-over"])
        assert path.exists()
        restart_resume.write_intent([])
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_a_boot_with_no_record_resumes_nothing_with_the_switch_off(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        """A crash writes no record: its chat keeps Resume and waits for the owner."""
        slot = _cut_off(_slot("crashed"))
        state = _mock_state(slot)
        _record(monkeypatch, tmp_path, None)

        assert await restart_resume.resume_after_restart(state, _cfg(enabled=False)) == 0
        assert quiet_dispatch == []

    @pytest.mark.asyncio
    async def test_a_planned_restarts_resume_counts_against_the_budget(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        """The budget is what bounds a gateway cycling through planned restarts: the
        record alone is read once per boot, which bounds nothing across boots."""
        import time

        slot = _cut_off(_slot("cut"))
        slot._auto_resume_stamps = [time.time() - 10.0 * n for n in range(5)]
        state = _mock_state(slot)
        _record(monkeypatch, tmp_path, ["cut"])

        assert await restart_resume.resume_after_restart(state, _cfg(enabled=False)) == 0
        assert quiet_dispatch == []
        assert len(slot._auto_resume_stamps) == 5

    @pytest.mark.asyncio
    async def test_the_switch_turning_off_leaves_a_planned_chat_running(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        """The live switch read before each dispatch spares the crash-cut chats; a chat
        the record names never needed the switch and goes on."""
        crash = _cut_off(_slot("crash"))
        planned = _cut_off(_slot("planned"))
        state = _mock_state(crash, planned)
        _record(monkeypatch, tmp_path, ["planned"])
        monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: False)

        assert await restart_resume.resume_after_restart(state, _cfg(enabled=True)) == 1
        assert [c["slot"] for c in quiet_dispatch] == ["planned"]
        assert crash._restart_cut_turn is False, "this boot's reading is spent for it"

    @pytest.mark.asyncio
    async def test_each_dispatch_waits_for_the_memory_preparation_first(
        self, tmp_path, monkeypatch, quiet_dispatch, _no_memory_wait
    ):
        state = _mock_state(_cut_off(_slot("a")), _cut_off(_slot("b")))
        _record(monkeypatch, tmp_path, ["a", "b"])

        assert await restart_resume.resume_after_restart(state, _cfg(enabled=False)) == 2
        assert _no_memory_wait.await_count == 2

    @pytest.mark.asyncio
    async def test_at_most_the_resume_all_bound_of_turns_run_at_once(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        """The fifth chat waits for an earlier turn to end; a turn hands its permit
        back when its task is done (the Resume-all follow)."""
        from kiro_crew.dashboard.chat_resume_all import RESUME_ALL_MAX_RUNNING

        names = [f"c{i}" for i in range(RESUME_ALL_MAX_RUNNING + 2)]
        started: list[str] = []
        gates: dict[str, asyncio.Event] = {}
        # ``reached[n]`` is set the moment the n-th chat is dispatched.
        reached = {n: asyncio.Event() for n in range(1, len(names) + 1)}

        async def _turn(gate: asyncio.Event) -> None:
            await gate.wait()

        async def fake_continue(state, slot, **kwargs):
            started.append(slot.key)
            gate = gates[slot.key] = asyncio.Event()
            slot.task = asyncio.create_task(_turn(gate))
            reached[len(started)].set()
            return "resumed"

        async def _let_the_pass_run() -> None:
            # Yields, not time: each one lets the pass take one more step, so a
            # pass with no bound would reach the next dispatch within a few.
            for _ in range(10):
                await asyncio.sleep(0)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.continue_slot_turn", fake_continue)
        state = _mock_state(*(_cut_off(_slot(n)) for n in names))
        _record(monkeypatch, tmp_path, names)

        run = asyncio.create_task(restart_resume.resume_after_restart(state, _cfg(enabled=False)))
        await await_until(reached[RESUME_ALL_MAX_RUNNING])
        await _let_the_pass_run()
        assert len(started) == RESUME_ALL_MAX_RUNNING, "the bound held while every turn ran"
        gates[names[0]].set()  # one turn ends: its permit goes to the next chat
        await await_until(reached[RESUME_ALL_MAX_RUNNING + 1])
        await _let_the_pass_run()
        assert len(started) == RESUME_ALL_MAX_RUNNING + 1
        gates[names[1]].set()
        await await_until(reached[len(names)])
        for gate in gates.values():
            gate.set()
        assert await await_until(run, timeout=FALLBACK_TIMEOUT_SECS) == len(names)
        assert started == names
