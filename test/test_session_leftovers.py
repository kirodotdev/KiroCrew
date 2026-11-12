"""Leftover processes: a runtime's session members its process tree does not reach.

A job a timed-out shell command left running is reparented to init and keeps the
runtime's session id. These tests pin how the session members are read (Linux
``/proc`` fixture, macOS ``getsid`` filter), how leftovers are told apart from the
live tree, and what the RSS tick does with them: end them after a recycle once the
runtime is gone, and during a turn report them once, ending them only when
``session.watchdog_end_leftovers_mid_turn`` is on.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew import platform_compat_proctable as proctable
from kiro_crew import session_cleanup_leftovers as leftovers_mod
from kiro_crew import session_pid
from kiro_crew.session_pid_rss import Leftover, SessionLeftovers


def _write_stat(root, pid: int, ppid: int, pgrp: int, sid: int, state: str = "S") -> None:
    (root / str(pid)).mkdir()
    (root / str(pid) / "stat").write_text(f"{pid} (p) {state} {ppid} {pgrp} {sid} 0\n")


class TestSessionMembers:
    def test_linux_lists_running_members_of_one_session(self, tmp_path) -> None:
        _write_stat(tmp_path, 100, 1, 100, 100)
        _write_stat(tmp_path, 300, 1, 100, 100)  # leftover, same group
        _write_stat(tmp_path, 301, 1, 301, 100)  # leftover under set -m
        _write_stat(tmp_path, 302, 1, 302, 100, state="Z")  # finished
        _write_stat(tmp_path, 400, 1, 400, 400)  # another session
        members = proctable.linux_session_members(100, proc_root=tmp_path)
        assert members is not None
        assert sorted(members) == [100, 300, 301]

    def test_macos_filters_every_process_by_getsid(self, monkeypatch) -> None:
        rows = [
            proctable.DarwinKinfoProc(pid=100, zombie=False, start_id="1.0"),
            proctable.DarwinKinfoProc(pid=300, zombie=False, start_id="2.0"),
            proctable.DarwinKinfoProc(pid=302, zombie=True, start_id="3.0"),
            proctable.DarwinKinfoProc(pid=400, zombie=False, start_id="4.0"),
        ]
        sids = {100: 100, 300: 100, 302: 100, 400: 400}
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(proctable, "_darwin_kinfo_members", lambda selector, arg: rows)
        monkeypatch.setattr(proctable.os, "getsid", lambda pid: sids[pid], raising=False)
        assert proctable.darwin_session_pids(100) == [100, 300]

    def test_macos_footprint_counts_session_members_off_the_child_lists(self, monkeypatch) -> None:
        mib = 1024 * 1024
        footprint = {100: 10 * mib, 200: 20 * mib, 300: 300 * mib, 310: 5 * mib}
        children = {100: [200], 300: [310]}
        monkeypatch.setattr(platform_compat, "IS_MACOS", True)
        monkeypatch.setattr(proctable, "_darwin_process_phys_footprint_bytes", footprint.get)
        monkeypatch.setattr(platform_compat, "darwin_child_pids", lambda p: children.get(p, []))
        monkeypatch.setattr(proctable, "darwin_session_pids", lambda sid: [100, 200, 300])
        assert proctable.darwin_footprint_tree_mb(100) == 335.0


class TestFindSessionLeftovers:
    @pytest.fixture
    def linux(self, monkeypatch):
        pc = session_pid.platform_compat
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "get_process_start_id", lambda pid: f"s{pid}")
        monkeypatch.setattr(
            pc, "linux_session_members", lambda sid: {100: 1, 200: 1, 300: 1, 301: 1}
        )
        monkeypatch.setattr(
            pc, "linux_process_name", lambda pid: "py\x1b[31m](x)" if pid == 300 else "sleep"
        )
        monkeypatch.setattr(session_pid, "_build_child_map", lambda *a, **k: {100: [200]})
        monkeypatch.setattr(session_pid, "_read_rss_pages", lambda pid, proc_root=None: 0)
        return pc

    def test_members_outside_the_live_tree_are_the_leftovers(self, linux) -> None:
        found = session_pid.find_session_leftovers(100)
        assert found is not None
        assert found.root_start == "s100"
        assert [item.pid for item in found.items] == [300, 301]
        # A name the process chose for itself reaches a log and a chat notice only
        # as plain letters, digits and ._+-.
        assert found.items[0].name == "py31mx"

    def test_a_root_that_changed_identity_reads_nothing(self, linux, monkeypatch) -> None:
        starts = iter(["s100", "other"])
        monkeypatch.setattr(
            linux, "get_process_start_id", lambda pid: next(starts) if pid == 100 else f"s{pid}"
        )
        assert session_pid.find_session_leftovers(100) is None

    def test_the_gateway_itself_is_never_a_root(self, linux) -> None:
        import os

        assert session_pid.find_session_leftovers(os.getpid()) is None


class TestEndSessionLeftovers:
    _FOUND = SessionLeftovers("s100", [Leftover(300, "s300", 900, "python3")])

    def test_nothing_is_signalled_while_the_root_still_runs(self, monkeypatch) -> None:
        pc = session_pid.platform_compat
        terminate = MagicMock(return_value=1)
        monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "s100")
        monkeypatch.setattr(pc, "terminate_pids_pinned", terminate)
        ended = session_pid.end_session_leftovers(100, self._FOUND, only_if_root_gone=True)
        assert ended == 0
        terminate.assert_not_called()

    def test_a_gone_root_ends_the_members_still_in_its_session(self, monkeypatch) -> None:
        pc = session_pid.platform_compat
        terminate = MagicMock(return_value=1)
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "get_process_start_id", lambda pid: None)
        monkeypatch.setattr(pc, "linux_session_members", lambda sid: {300: 1})
        monkeypatch.setattr(pc, "terminate_pids_pinned", terminate)
        ended = session_pid.end_session_leftovers(100, self._FOUND, only_if_root_gone=True)
        assert ended == 1
        terminate.assert_called_once_with([(300, "s300")], protected_pids=(100,))


def _manager(*, end_mid_turn: object = False):
    from kiro_crew.session import SessionManager

    cfg = MagicMock()
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    cfg.session.watchdog_rss_max_mb = 1000
    cfg.session.watchdog_end_leftovers_mid_turn = end_mid_turn
    manager = SessionManager(cfg=cfg, provider_factory=None)
    manager.get_pid = MagicMock(return_value=4242)
    return manager


def _stub(*, busy: bool):
    sess = MagicMock()
    sess.semaphore.locked.return_value = busy
    return sess


_FOUND = SessionLeftovers("s4242", [Leftover(5000, "s5000", 1800, "python3")])


async def _tick(manager, end=None, *, linux: bool = True) -> MagicMock:
    end = end or MagicMock(return_value=1)
    with (
        patch("kiro_crew.session._build_child_map", return_value={}),
        patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        patch.object(session_pid.platform_compat, "IS_WINDOWS", False),
        patch.object(session_pid.platform_compat, "IS_LINUX", linux),
        patch("kiro_crew.session_pid.find_session_leftovers", return_value=_FOUND),
        patch("kiro_crew.session_pid.end_session_leftovers", end),
    ):
        await manager._rss_threshold_check()
    return end


class TestRssTickLeftovers:
    @pytest.mark.asyncio
    async def test_a_recycle_ends_the_leftovers_once_the_runtime_is_gone(self) -> None:
        manager = _manager()
        manager._sessions["dashboard:x"] = _stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        end = await _tick(manager)
        end.assert_called_once_with(4242, _FOUND, only_if_root_gone=True)

    @pytest.mark.asyncio
    async def test_a_turn_is_reported_once_and_nothing_is_ended_by_default(self) -> None:
        manager = _manager()
        manager._sessions["dashboard:x"] = _stub(busy=True)
        manager.reset = AsyncMock(return_value=True)
        notices: list[tuple[str, str]] = []
        manager.on_leftover_memory = lambda key, text: notices.append((key, text))
        end = await _tick(manager)
        end = await _tick(manager, end)
        manager.reset.assert_not_awaited()
        end.assert_not_called()
        assert [key for key, _ in notices] == ["dashboard:x"]
        assert "pid 5000 python3 (1800 MB)" in notices[0][1]
        assert "session.watchdog_end_leftovers_mid_turn" in notices[0][1]

    @pytest.mark.asyncio
    async def test_the_next_turn_gets_its_own_notice(self) -> None:
        """A turn's release rearms the notice, even when the next turn starts before
        any tick sees the session idle."""
        import asyncio

        manager = _manager()
        sess = _stub(busy=True)
        sess.semaphore = asyncio.Semaphore(0)
        manager._sessions["dashboard:x"] = sess
        manager.reset = AsyncMock(return_value=True)
        notices: list[str] = []
        manager.on_leftover_memory = lambda key, text: notices.append(key)
        await _tick(manager)
        manager.release("dashboard:x")
        await sess.semaphore.acquire()  # the next turn
        await _tick(manager)
        assert notices == ["dashboard:x", "dashboard:x"]

    @pytest.mark.asyncio
    async def test_the_opt_in_ends_them_mid_turn(self) -> None:
        manager = _manager(end_mid_turn=True)
        manager._sessions["dashboard:x"] = _stub(busy=True)
        manager.reset = AsyncMock(return_value=True)
        end = await _tick(manager)
        manager.reset.assert_not_awaited()
        end.assert_called_once_with(4242, _FOUND, only_if_root_gone=False)

    @pytest.mark.asyncio
    async def test_turning_the_opt_in_on_mid_turn_ends_already_reported_ones(self) -> None:
        manager = _manager()
        manager._sessions["dashboard:x"] = _stub(busy=True)
        manager.reset = AsyncMock(return_value=True)
        end = await _tick(manager)
        end.assert_not_called()
        manager._cfg.session.watchdog_end_leftovers_mid_turn = True
        end = await _tick(manager, end)
        end.assert_called_once_with(4242, _FOUND, only_if_root_gone=False)

    @pytest.mark.asyncio
    async def test_a_host_that_cannot_end_them_promises_nothing(self) -> None:
        manager = _manager(end_mid_turn=True)
        manager._sessions["dashboard:x"] = _stub(busy=True)
        manager.reset = AsyncMock(return_value=True)
        notices: list[str] = []
        manager.on_leftover_memory = lambda key, text: notices.append(text)
        end = await _tick(manager, linux=False)
        end.assert_not_called()
        assert "cannot end them safely on this host" in notices[0]
        assert "watchdog_end_leftovers_mid_turn" not in notices[0]

    def test_only_a_real_true_turns_the_opt_in_on(self) -> None:
        on = SimpleNamespace(session=SimpleNamespace(watchdog_end_leftovers_mid_turn=True))
        stray = SimpleNamespace(session=SimpleNamespace(watchdog_end_leftovers_mid_turn="yes"))
        assert leftovers_mod.end_mid_turn_enabled(on) is True
        assert leftovers_mod.end_mid_turn_enabled(stray) is False
        assert leftovers_mod.end_mid_turn_enabled(SimpleNamespace()) is False


def test_the_setting_ships_off() -> None:
    from kiro_crew.config.sections import SessionConfig

    assert SessionConfig().watchdog_end_leftovers_mid_turn is False
