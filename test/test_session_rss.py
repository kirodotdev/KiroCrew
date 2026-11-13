"""Tests for the RSS-threshold recycle hook.

Covers tree accumulation, the real /proc parsing primitives against a fake /proc tree,
per-PID pages summed then truncated to MiB once, the ``_rss_threshold_check`` sweep and
its guards, and ``reset()``'s ``expect_session`` and ``skip_if_busy`` guards.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import session, session_pid


def _make_manager(rss_max_mb: int):
    """Build a SessionManager with a MagicMock cfg and an explicit int RSS
    threshold (a bare MagicMock attribute would be treated as disabled)."""
    from kiro_crew.session import SessionManager

    cfg = MagicMock()
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    cfg.session.watchdog_rss_max_mb = rss_max_mb
    return SessionManager(cfg=cfg, provider_factory=None)


def _session_stub(*, busy: bool):
    sess = MagicMock()
    sess.semaphore = MagicMock()
    sess.semaphore.locked.return_value = busy
    return sess


def _warm_provider(pid: int):
    """A warm-pool provider stub whose runtime tree is read via ``client._pid``.

    Mirrors how the pool reads a spare's pid (``provider.client._pid``); the
    RSS spare pass measures that pid's tree and discards over-ceiling ones.
    """
    provider = MagicMock()
    provider.client._pid = pid
    return provider


def _pool_warm_provider(manager, pid: int):
    """Enqueue a warm-pool spare the way ``warm_providers`` snapshots it."""
    provider = _warm_provider(pid)
    manager._warm_pool.put_nowait((provider, 0.0))
    return provider


def _rss_recycled(manager) -> dict:
    """The sweep's own record of the sessions it recycled this window."""
    return manager._cleanup_boundary().state.rss_recycled


def _mib_of_pages(total_pages: int) -> int:
    return (total_pages * session_pid._PAGE_SIZE) // (1024 * 1024)


class TestGetSessionRssMb:
    @pytest.fixture(autouse=True)
    def _off_windows_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default this class to the /proc route.

        The Windows dispatch keys off ``platform_compat.IS_WINDOWS``, not
        ``sys.platform``, so on a Windows host the stubs would never be reached.
        """
        monkeypatch.setattr(session_pid.platform_compat, "IS_WINDOWS", False)

    def test_windows_measures_the_tree_rather_than_returning_zero(self) -> None:
        """Windows has no /proc, but it MUST still measure.

        Returning 0 there made the configured ``watchdog_rss_max_mb`` ceiling
        unreachable, so a session tree could grow without ever being recycled.
        """
        with (
            patch("kiro_crew.session_pid.sys.platform", "win32"),
            patch("kiro_crew.session_pid.platform_compat.IS_WINDOWS", True),
            patch(
                "kiro_crew.session_pid.platform_compat.proc_rss_tree_mb_for_pid",
                return_value=512.7,
            ),
        ):
            assert session_pid.get_session_rss_mb(100) == 512

    def test_windows_uses_the_lineage_validated_helper_not_a_raw_walk(self) -> None:
        """Toolhelp's PPID field is never cleared when a parent exits and Windows
        recycles PIDs, so a raw parent->child walk can attach an unrelated subtree
        to a recycled PID -- and recycle a HEALTHY session. The measurement must
        go through the helper that validates each edge against creation times."""
        with (
            patch("kiro_crew.session_pid.sys.platform", "win32"),
            patch("kiro_crew.session_pid.platform_compat.IS_WINDOWS", True),
            patch(
                "kiro_crew.session_pid.platform_compat.proc_rss_tree_mb_for_pid",
                return_value=1.0,
            ),
            patch(
                "kiro_crew.session_pid._build_child_map",
                side_effect=AssertionError("must not walk a raw Toolhelp parent map"),
            ),
        ):
            assert session_pid.get_session_rss_mb(100) == 1

    def test_windows_treats_an_unreadable_tree_as_zero(self) -> None:
        """None means "unknown"; the ceiling must not fire on a guess."""
        with (
            patch("kiro_crew.session_pid.sys.platform", "win32"),
            patch("kiro_crew.session_pid.platform_compat.IS_WINDOWS", True),
            patch(
                "kiro_crew.session_pid.platform_compat.proc_rss_tree_mb_for_pid",
                return_value=None,
            ),
        ):
            assert session_pid.get_session_rss_mb(100) == 0

    def test_accumulates_root_plus_descendants(self) -> None:
        # tree: 100 -> [200, 300]; 300 -> [400]
        child_map = {100: [200, 300], 300: [400]}
        pages = {100: 1000, 200: 2000, 300: 3000, 400: 4000}
        with (
            patch("kiro_crew.session_pid.sys.platform", "linux"),
            patch("kiro_crew.session_pid._build_child_map", return_value=child_map),
            patch(
                "kiro_crew.session_pid._read_rss_pages",
                side_effect=lambda p, proc_root=None: pages.get(p, 0),
            ),
        ):
            assert session_pid.get_session_rss_mb(100) == _mib_of_pages(10000)

    def test_exclude_pids_prunes_subtree(self) -> None:
        # Excluding 300 must also drop its child 400.
        child_map = {100: [200, 300], 300: [400]}
        pages = {100: 1000, 200: 2000, 300: 3000, 400: 4000}
        with (
            patch("kiro_crew.session_pid.sys.platform", "linux"),
            patch("kiro_crew.session_pid._build_child_map", return_value=child_map),
            patch(
                "kiro_crew.session_pid._read_rss_pages",
                side_effect=lambda p, proc_root=None: pages.get(p, 0),
            ),
        ):
            assert session_pid.get_session_rss_mb(100, exclude_pids={300}) == _mib_of_pages(3000)

    def test_cycle_safe(self) -> None:
        # Pathological cycle 100 -> 200 -> 100 must terminate; each counted once.
        child_map = {100: [200], 200: [100]}
        with (
            patch("kiro_crew.session_pid.sys.platform", "linux"),
            patch("kiro_crew.session_pid._build_child_map", return_value=child_map),
            patch(
                "kiro_crew.session_pid._read_rss_pages",
                side_effect=lambda p, proc_root=None: 5,
            ),
        ):
            assert session_pid.get_session_rss_mb(100) == _mib_of_pages(10)

    def test_sub_mib_per_pid_not_truncated_away(self) -> None:
        # Two sibling processes
        # each just over half a MiB. A per-PID ``// MiB`` truncates each to 0
        # (the old behaviour reported 0 for the tree); summing pages first and
        # truncating once yields >= 1 MiB.
        pages_per_mib = (1024 * 1024) // session_pid._PAGE_SIZE
        half = pages_per_mib // 2 + 1  # just over 0.5 MiB each
        # Precondition: each PID individually truncates to 0 MiB.
        assert (half * session_pid._PAGE_SIZE) // (1024 * 1024) == 0
        child_map = {100: [200]}
        pages = {100: half, 200: half}
        with (
            patch("kiro_crew.session_pid.sys.platform", "linux"),
            patch("kiro_crew.session_pid._build_child_map", return_value=child_map),
            patch(
                "kiro_crew.session_pid._read_rss_pages",
                side_effect=lambda p, proc_root=None: pages.get(p, 0),
            ),
        ):
            assert session_pid.get_session_rss_mb(100) >= 1

    def test_rss_mb_from_tree_uses_shared_map(self) -> None:
        # _rss_mb_from_tree consumes a prebuilt map (no /proc scan of its own).
        child_map = {100: [200, 300], 300: [400]}
        pages = {100: 1000, 200: 2000, 300: 3000, 400: 4000}
        with (
            patch("kiro_crew.session_pid.sys.platform", "linux"),
            patch(
                "kiro_crew.session_pid._read_rss_pages",
                side_effect=lambda p, proc_root=None: pages.get(p, 0),
            ),
            patch("kiro_crew.session_pid._build_child_map") as bm,
        ):
            got = session_pid._rss_mb_from_tree(100, child_map)
        assert got == _mib_of_pages(10000)
        bm.assert_not_called()  # never scans /proc itself


class TestProcParsingPrimitives:
    """Exercise the REAL /proc parsing against a fake
    /proc tree under tmp_path via the proc_root seam."""

    def test_read_rss_pages_parses_statm_resident_field(self, tmp_path) -> None:
        (tmp_path / "123").mkdir()
        # statm: size resident shared text lib data dt -> field[1] == resident
        (tmp_path / "123" / "statm").write_text("1000 2048 15 1 0 500 0\n")
        assert session_pid._read_rss_pages(123, proc_root=tmp_path) == 2048

    def test_read_rss_pages_missing_or_malformed_returns_zero(self, tmp_path) -> None:
        # no dir at all
        assert session_pid._read_rss_pages(999, proc_root=tmp_path) == 0
        # too few fields -> IndexError guard
        (tmp_path / "5").mkdir()
        (tmp_path / "5" / "statm").write_text("onlyone")
        assert session_pid._read_rss_pages(5, proc_root=tmp_path) == 0
        # non-numeric resident field -> ValueError guard
        (tmp_path / "6").mkdir()
        (tmp_path / "6" / "statm").write_text("x y z")
        assert session_pid._read_rss_pages(6, proc_root=tmp_path) == 0

    def test_build_child_map_parses_ppid_even_with_paren_comm(self, tmp_path) -> None:
        (tmp_path / "100").mkdir()
        (tmp_path / "100" / "stat").write_text("100 (bash) S 1 100 100 0 -1 0\n")
        # comm containing spaces AND parens must not fool the ppid parse
        (tmp_path / "200").mkdir()
        (tmp_path / "200" / "stat").write_text("200 (weird (proc) name) S 100 200 0 0\n")
        (tmp_path / "300").mkdir()
        (tmp_path / "300" / "stat").write_text("300 (child) S 200 300 0 0\n")
        (tmp_path / "not-a-pid").mkdir()  # non-numeric entry must be ignored
        child_map = session_pid._build_child_map(proc_root=tmp_path)
        assert child_map.get(1) == [100]
        assert child_map.get(100) == [200]
        assert child_map.get(200) == [300]

    def test_build_child_map_skips_unreadable_stat(self, tmp_path) -> None:
        (tmp_path / "100").mkdir()  # no stat file -> skipped, no crash
        (tmp_path / "200").mkdir()
        (tmp_path / "200" / "stat").write_text("200 (x) S 1 200 0 0\n")
        child_map = session_pid._build_child_map(proc_root=tmp_path)
        assert child_map.get(1) == [200]
        assert all(100 not in kids for kids in child_map.values())

    def test_session_members_count_a_job_reparented_to_init(self, tmp_path) -> None:
        """A job a timed-out shell command left behind is reparented to
        init but keeps the runtime's session id. The ceiling's map counts it in
        the runtime's tree; the default map (used by kill paths) does not."""
        # runtime 100 is its own session leader; 200 is its live child; 300 is
        # the leftover job (ppid 1, session 100); 400 is a stranger (session 400).
        stats = {100: (1, 100), 200: (100, 100), 300: (1, 100), 400: (1, 400)}
        for pid, (ppid, sid) in stats.items():
            (tmp_path / str(pid)).mkdir()
            (tmp_path / str(pid) / "stat").write_text(f"{pid} (p) S {ppid} {pid} {sid} 0\n")
            (tmp_path / str(pid) / "statm").write_text("9999 100 0 0 0 0 0\n")
        plain = session_pid._build_child_map(proc_root=tmp_path)
        assert 300 not in plain.get(100, [])
        members = session_pid._build_child_map(proc_root=tmp_path, session_members=True)
        assert sorted(members[100]) == [200, 300]
        assert 400 not in members.get(100, [])
        got = session_pid._rss_mb_from_tree(100, members, proc_root=tmp_path)
        assert got == _mib_of_pages(300)

    def test_get_session_rss_mb_end_to_end_real_parse(self, tmp_path) -> None:
        # root 100 -> child 200, using the REAL _build_child_map + _read_rss_pages.
        for pid, ppid, resident in ((100, 1, 300), (200, 100, 300)):
            (tmp_path / str(pid)).mkdir()
            (tmp_path / str(pid) / "stat").write_text(f"{pid} (p) S {ppid} {pid} 0 0\n")
            (tmp_path / str(pid) / "statm").write_text(f"9999 {resident} 0 0 0 0 0\n")
        with patch("kiro_crew.session_pid.sys.platform", "linux"):
            expected = _mib_of_pages(300 + 300)
            assert session_pid.get_session_rss_mb(100, proc_root=tmp_path) == expected


class TestWindowsRssPrimitives:
    """Why the ``/proc`` primitives stay ``/proc``-only.

    Windows measures through a lineage-validating helper instead: a raw Toolhelp
    parent map with a recycled PID would sum an unrelated subtree and recycle a
    healthy session.
    """

    def test_the_child_map_has_no_windows_branch_by_design(self) -> None:
        """A raw Toolhelp parent map is deliberately NOT used here.

        ``th32ParentProcessID`` is never cleared when a parent exits. Windows uses its
        own lineage-validated route in ``get_session_rss_mb``.
        """
        import inspect

        from kiro_crew import platform_compat

        for function in (session_pid._build_child_map, platform_compat.proc_child_map):
            assert "_windows_process_parent_map" not in inspect.getsource(function)


@pytest.mark.usefixtures("rss_runtimes_read_under_ceiling")
class TestRssThresholdCheck:
    @pytest.fixture(autouse=True)
    def _on_the_proc_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the hook's /proc branch for the behaviour tests below.

        They stub the /proc helpers, which only the /proc branch calls; a Windows host
        would otherwise measure real trees, all under threshold.
        """
        monkeypatch.setattr(session.platform_compat, "IS_WINDOWS", False)

    def test_shipped_default_reaches_the_enforcement_point(self) -> None:
        """A manager built from the shipped SessionConfig default holds the
        shipped ceiling, so ``_rss_threshold_check`` enforces it out of the box."""
        from kiro_crew.config.sections import DEFAULT_WATCHDOG_RSS_MAX_MB, SessionConfig

        manager = _make_manager(rss_max_mb=SessionConfig().watchdog_rss_max_mb)
        assert manager._rss_max_mb == DEFAULT_WATCHDOG_RSS_MAX_MB == 4096

    @pytest.mark.asyncio
    async def test_explicit_zero_disables(self) -> None:
        manager = _make_manager(rss_max_mb=0)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock()
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=99999) as g,
        ):
            await manager._rss_threshold_check()
        g.assert_not_called()
        manager.reset.assert_not_awaited()
        assert _rss_recycled(manager) == {}

    @pytest.mark.asyncio
    async def test_over_threshold_session_is_reset(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        stub = _session_stub(busy=False)
        manager._sessions["dashboard:x"] = stub
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        manager.reset.assert_awaited_once()
        ca = manager.reset.await_args
        assert ca.args[0] == "dashboard:x"
        # New signature: victim identity + busy guard handed to reset() so the
        # kill/skip decision is atomic under the lock.
        assert ca.kwargs["skip_if_busy"] is True
        assert ca.kwargs["expect_session"] is stub

    @pytest.mark.asyncio
    async def test_child_map_built_once_per_tick(self) -> None:
        # review-bot perf finding: _build_child_map scans all of /proc, so it must
        # run once per sweep, not once per candidate.
        #
        # Distinct pids: the measurement is cached per RUNTIME, so two candidates
        # on one pid would legitimately be one walk and could not show that the
        # per-candidate measurement still happens. Co-tenancy has its own test.
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(side_effect={"dashboard:a": 11, "dashboard:b": 22}.get)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}) as bm,
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048) as rt,
        ):
            await manager._rss_threshold_check()
        assert bm.call_count == 1  # one /proc scan for the whole tick
        assert rt.call_count == 2  # measured once per candidate
        recycled = {key: rec.rss for key, rec in _rss_recycled(manager).items()}
        assert recycled == {"dashboard:a": 2048, "dashboard:b": 2048}

    @pytest.mark.asyncio
    async def test_windows_measures_per_candidate_without_a_shared_map(self) -> None:
        """Windows cannot share one snapshot across candidates.

        A raw Toolhelp parent map is unsafe (stale PPIDs + recycled PIDs would
        attach an unrelated subtree and recycle a healthy session), so each tree
        is measured through the lineage-validating route instead. Paying one
        enumeration per candidate is the deliberate trade.
        """
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(side_effect={"dashboard:a": 11, "dashboard:b": 22}.get)
        # Overrides the class fixture's /proc pin: this is the Windows branch.
        with (
            patch.object(session.platform_compat, "IS_WINDOWS", True),
            patch(
                "kiro_crew.session._build_child_map",
                side_effect=AssertionError("must not build a raw Toolhelp parent map"),
            ),
            patch("kiro_crew.session.get_session_rss_mb", return_value=2048) as gs,
        ):
            await manager._rss_threshold_check()
        assert gs.call_count == 2
        assert manager.reset.await_count == 2
        recycled = {key: rec.rss for key, rec in _rss_recycled(manager).items()}
        assert recycled == {"dashboard:a": 2048, "dashboard:b": 2048}

    @pytest.mark.asyncio
    async def test_one_failed_victim_does_not_skip_the_rest(self) -> None:
        # reset() raising for the first victim must not prevent the second from
        # being recycled — the per-victim guard isolates failures.
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        reset_calls: list[str] = []

        async def _reset(key, *, expect_session=None, skip_if_busy=False, skip_if_injecting=False):
            reset_calls.append(key)
            if key == "dashboard:a":
                raise RuntimeError("boom")
            return True

        manager.reset = _reset  # type: ignore[assignment]
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()  # must not raise
        assert reset_calls == ["dashboard:a", "dashboard:b"]

    @pytest.mark.asyncio
    async def test_co_tenants_are_measured_once_and_recycle_one_session(self) -> None:
        """Two sessions on one runtime: the tree is measured once and one is recycled.

        Co-tenants share one tree's figure, so all cross together; resetting all of
        them discards their work while the process survives any remaining tenant.
        """
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)  # one runtime, two tenants
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048) as rt,
        ):
            await manager._rss_threshold_check()
        assert rt.call_count == 1  # one walk per DISTINCT pid
        assert manager.reset.await_count == 1
        recycled = {key: rec.rss for key, rec in _rss_recycled(manager).items()}
        assert recycled == {"dashboard:a": 2048}

    @pytest.mark.asyncio
    async def test_distinct_runtimes_over_threshold_are_each_recycled(self) -> None:
        """The one-per-runtime rule is keyed on the pid, so two sessions on two
        runtimes are both recycled -- the 1:1 case must be untouched."""
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(side_effect={"dashboard:a": 11, "dashboard:b": 22}.get)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048) as rt,
        ):
            await manager._rss_threshold_check()
        assert rt.call_count == 2
        assert manager.reset.await_count == 2
        recycled = {key: rec.rss for key, rec in _rss_recycled(manager).items()}
        assert recycled == {"dashboard:a": 2048, "dashboard:b": 2048}

    @pytest.mark.asyncio
    async def test_a_skipped_co_tenant_lets_the_next_one_be_recycled(self) -> None:
        """The budget is one SUCCESSFUL reset per runtime, not one attempt.

        A declined victim reclaimed nothing, so the tick may recycle a co-tenant;
        otherwise one permanently guarded tenant makes the ceiling unreachable.
        """
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        reset_calls: list[str] = []

        async def _reset(key, *, expect_session=None, skip_if_busy=False, skip_if_injecting=False):
            reset_calls.append(key)
            return key == "dashboard:b"  # 'a' declines, 'b' recycles

        manager.reset = _reset  # type: ignore[assignment]
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        assert reset_calls == ["dashboard:a", "dashboard:b"]

    @pytest.mark.asyncio
    async def test_recycle_fires_notification_callback(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        cb = AsyncMock()
        manager.set_recycle_callback(cb)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        cb.assert_awaited_once()
        # key positional + reason kwarg carrying the MB figure
        args, kwargs = cb.await_args
        assert args[0] == "dashboard:x"
        assert "2048" in kwargs["reason"]

    @pytest.mark.asyncio
    async def test_no_notification_when_reset_reports_noop(self) -> None:
        # collect->reset race: session was non-busy at collection, but reset()
        # reports False (it became busy, or was reset+recreated under a reused
        # key, and reset()'s atomic guard declined). No misleading recycle notice
        # must fire for it, and remaining victims must still be processed.
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        cb = AsyncMock()
        manager.set_recycle_callback(cb)
        reset_calls: list[str] = []

        async def _reset(key, *, expect_session=None, skip_if_busy=False, skip_if_injecting=False):
            reset_calls.append(key)
            return key == "dashboard:b"  # 'a' is a no-op, 'b' recycled

        manager.reset = _reset  # type: ignore[assignment]
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        assert set(reset_calls) == {"dashboard:a", "dashboard:b"}
        cb.assert_awaited_once()
        assert cb.await_args.args[0] == "dashboard:b"

    @pytest.mark.asyncio
    async def test_under_threshold_session_is_kept_without_notification(self) -> None:
        manager = _make_manager(rss_max_mb=4096)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        cb = AsyncMock()
        manager.set_recycle_callback(cb)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=512) as g,
        ):
            await manager._rss_threshold_check()
        g.assert_called_once()
        manager.reset.assert_not_awaited()
        cb.assert_not_awaited()
        assert _rss_recycled(manager) == {}

    @pytest.mark.parametrize(
        "key, busy, pid",
        [
            pytest.param(
                next(iter(session._PERSISTENT_KEYS)), False, 4242, id="persistent_key_is_protected"
            ),
            # Channel sessions are protected from idle expiry; RSS recycle must
            # skip them too and never even measure them.
            pytest.param(
                f"{session._CHANNEL_PREFIX}team-eng", False, 4242, id="channel_key_is_protected"
            ),
            pytest.param("dashboard:x", False, None, id="missing_pid_is_skipped"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_ineligible_session_is_never_measured(self, key, busy, pid) -> None:
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions[key] = _session_stub(busy=busy)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=pid)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=999999) as g,
        ):
            await manager._rss_threshold_check()
        g.assert_not_called()
        manager.reset.assert_not_awaited()
        assert _rss_recycled(manager) == {}

    @pytest.mark.asyncio
    async def test_a_busy_session_is_measured_but_never_reset(self) -> None:
        """A turn in flight is measured so its leftovers can be reported, and is
        never recycled (``session_cleanup_leftovers``)."""
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=True)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=999999) as g,
            patch("kiro_crew.session_pid.find_session_leftovers", return_value=None),
        ):
            await manager._rss_threshold_check()
        g.assert_called_once()
        manager.reset.assert_not_awaited()
        assert _rss_recycled(manager) == {}

    @pytest.mark.asyncio
    async def test_attached_subagents_protect_the_parent(self, caplog) -> None:
        """An idle parent over the ceiling is kept while children are attached.

        With session sharing on, a parent's sub-agents run on the parent's
        runtime after its own turn ended, so the semaphore is free and the
        RSS sweep would otherwise reset the runtime under them.
        """
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        probe = MagicMock(return_value=True)
        manager.set_subagent_probe(probe)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
            caplog.at_level(logging.DEBUG, logger="kiro_crew.session"),
        ):
            await manager._rss_threshold_check()
        probe.assert_called_once_with("dashboard:x")
        manager.reset.assert_not_awaited()
        assert _rss_recycled(manager) == {}
        assert any("has attached sub-agent work; skipping" in m for m in caplog.messages)

    @pytest.mark.asyncio
    async def test_parent_without_subagents_is_reset(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        stub = _session_stub(busy=False)
        manager._sessions["dashboard:x"] = stub
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        probe = MagicMock(return_value=False)
        manager.set_subagent_probe(probe)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        probe.assert_called_once_with("dashboard:x")
        manager.reset.assert_awaited_once()
        assert manager.reset.await_args.args[0] == "dashboard:x"
        assert [(key, rec.rss) for key, rec in _rss_recycled(manager).items()] == [
            ("dashboard:x", 2048)
        ]

    @pytest.mark.asyncio
    async def test_raising_subagent_probe_counts_as_attached(self, caplog) -> None:
        """A probe that cannot see the children keeps the session (fail closed)."""
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        manager.set_subagent_probe(MagicMock(side_effect=RuntimeError("registry gone")))
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
            caplog.at_level(logging.DEBUG, logger="kiro_crew.session"),
        ):
            await manager._rss_threshold_check()
        manager.reset.assert_not_awaited()
        assert _rss_recycled(manager) == {}
        assert any("has attached sub-agent work; skipping" in m for m in caplog.messages)

    @pytest.mark.asyncio
    async def test_no_probe_installed_recycles_as_before(self) -> None:
        """A manager with no dashboard has no children: the ceiling still applies."""
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        assert manager._subagent_probe is None
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        manager.reset.assert_awaited_once()

    @staticmethod
    def _clocked(manager, clock: list[float]):
        from dataclasses import replace

        cleanup = manager._cleanup_boundary()
        cleanup._deps = replace(cleanup._deps, monotonic=lambda: clock[0])
        return cleanup

    @pytest.mark.asyncio
    async def test_crossing_again_after_a_recycle_warns_once_per_cycle(self, caplog) -> None:
        """A replacement that climbs straight back over the ceiling is named once,
        with the ceiling, the pre-recycle figure and the elapsed time -- not on
        every later tick that still reads over the ceiling."""
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        clock = [5000.0]
        self._clocked(manager, clock)
        # The first recycle succeeds; the next two crossings are declined, so
        # the session stays over the ceiling across ticks.
        manager.reset = AsyncMock(side_effect=[True, False, False])
        rss = [2048]
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", side_effect=lambda *_: rss[0]),
            caplog.at_level("WARNING"),
        ):
            await manager._rss_threshold_check()
            assert "rebound" not in caplog.text
            clock[0] += 120.0
            rss[0] = 1900
            await manager._rss_threshold_check()
            clock[0] += 120.0
            await manager._rss_threshold_check()
        rebounds = [r.getMessage() for r in caplog.records if "rebound" in r.getMessage()]
        assert rebounds == [
            "RSS recycle rebound: session dashboard:x tree rss=1900MB is back above "
            "1000MB 120s after a recycle at 2048MB; "
            "session.watchdog_rss_max_mb may be set too low for this workload"
        ]

    @pytest.mark.asyncio
    async def test_each_recycle_cycle_gets_its_own_warning(self, caplog) -> None:
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        clock = [5000.0]
        self._clocked(manager, clock)
        manager.reset = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
            caplog.at_level("WARNING"),
        ):
            for _ in range(3):
                await manager._rss_threshold_check()
                clock[0] += 60.0
        assert sum("rebound" in r.getMessage() for r in caplog.records) == 2

    @pytest.mark.asyncio
    async def test_no_rebound_warning_without_a_successful_recycle(self, caplog) -> None:
        """A crossing the guards declined reclaimed nothing, so the next crossing
        is not a replacement climbing back."""
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        self._clocked(manager, [5000.0])
        manager.reset = AsyncMock(return_value=False)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
            caplog.at_level("WARNING"),
        ):
            await manager._rss_threshold_check()
            await manager._rss_threshold_check()
        assert "rebound" not in caplog.text

    @pytest.mark.asyncio
    async def test_crossing_after_the_window_is_not_a_rebound(self, caplog) -> None:
        from kiro_crew.session_rss_rebound import RSS_REBOUND_WINDOW_SECS

        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        clock = [5000.0]
        cleanup = self._clocked(manager, clock)
        manager.reset = AsyncMock(return_value=True)
        rss = [2048]
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", side_effect=lambda *_: rss[0]),
            caplog.at_level("WARNING"),
        ):
            await manager._rss_threshold_check()
            # Under the ceiling past the window: the record is pruned anyway.
            clock[0] += RSS_REBOUND_WINDOW_SECS + 1.0
            rss[0] = 500
            await manager._rss_threshold_check()
            assert cleanup.state.rss_recycled == {}
            rss[0] = 2048
            await manager._rss_threshold_check()
        assert "rebound" not in caplog.text

    def _rebounding(self, manager, clock: list[float]):
        """Wire *manager* with one idle session, a clock and both notice seams.

        The reset double leaves the session in place, which is what a fresh
        process restoring a history that alone is over the ceiling looks like
        to the next tick. Returns the recycle notices and the hold notices.
        """
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        self._clocked(manager, clock)
        manager.reset = AsyncMock(return_value=True)
        recycled = AsyncMock()
        manager.set_recycle_callback(recycled)
        held: list = []
        manager.on_rss_hold = lambda key, hold: held.append((key, hold))
        return recycled, held

    @pytest.mark.parametrize("footprint", [1500, 2800], ids=["under-twice", "over-twice"])
    @pytest.mark.asyncio
    async def test_a_session_that_keeps_rebounding_is_held_and_the_chat_told_once(
        self, caplog, footprint
    ) -> None:
        """A fresh process that alone sits over the ceiling is recycled again after
        every turn unless the recycle stops. The third rebound in a row is held:
        no more resets, one WARNING and one notice with the figures, also when
        the footprint is over twice the ceiling."""
        manager = _make_manager(rss_max_mb=1000)
        clock = [5000.0]
        recycled, held = self._rebounding(manager, clock)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=footprint),
            caplog.at_level("WARNING"),
        ):
            for _ in range(6):
                await manager._rss_threshold_check()
                clock[0] += 60.0
        assert manager.reset.await_count == 3
        from kiro_crew.session_rss_rebound import RssHold

        hold = RssHold(rss_mb=footprint, ceiling_mb=1000, limit_mb=2 * footprint, recycles=3)
        assert held == [("dashboard:x", hold)]
        assert sum("RSS recycle held" in r.getMessage() for r in caplog.records) == 1
        reasons = [c.kwargs["reason"] for c in recycled.await_args_list]
        assert reasons[0] == f"memory limit ({footprint}MB)"
        # A rebound recycle names the setting before the hold does.
        assert all("session.watchdog_rss_max_mb of 1000MB" in r for r in reasons[1:])
        assert len(reasons) == 3

    @pytest.mark.asyncio
    async def test_a_held_session_that_doubles_its_footprint_is_recycled(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        clock = [5000.0]
        rss = [1500]
        _recycled, held = self._rebounding(manager, clock)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", side_effect=lambda *_: rss[0]),
        ):
            for _ in range(4):
                await manager._rss_threshold_check()
                clock[0] += 60.0
            assert manager.reset.await_count == 3
            rss[0] = 3000  # exactly twice the held footprint: still held
            await manager._rss_threshold_check()
            assert manager.reset.await_count == 3
            rss[0] = 3001
            await manager._rss_threshold_check()
        assert manager.reset.await_count == 4
        assert len(held) == 1

    @pytest.mark.asyncio
    async def test_the_hold_ends_with_the_rebound_window(self) -> None:
        from kiro_crew.session_rss_rebound import RSS_REBOUND_WINDOW_SECS

        manager = _make_manager(rss_max_mb=1000)
        clock = [5000.0]
        recycled, _held = self._rebounding(manager, clock)
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=1500),
        ):
            for _ in range(4):
                await manager._rss_threshold_check()
                clock[0] += 60.0
            assert manager.reset.await_count == 3
            clock[0] += RSS_REBOUND_WINDOW_SECS
            await manager._rss_threshold_check()
        assert manager.reset.await_count == 4
        assert recycled.await_args.kwargs["reason"] == "memory limit (1500MB)"

    @pytest.mark.asyncio
    async def test_a_declined_rebound_keeps_the_count(self) -> None:
        """A rebound the guards decline reclaimed nothing, but it was still a
        rebound: the next tick retries it at the same count, so a decline can
        only delay the hold, never restart the count."""
        manager = _make_manager(rss_max_mb=1000)
        clock = [5000.0]
        _recycled, held = self._rebounding(manager, clock)
        manager.reset = AsyncMock(side_effect=[True, False, True, True])
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=1500),
        ):
            for _ in range(6):
                await manager._rss_threshold_check()
                clock[0] += 60.0
        assert manager.reset.await_count == 4
        assert [hold.recycles for _key, hold in held] == [3]


class TestRssFootprint:
    """A runtime the watchdog finds already over the ceiling is held, not recycled.

    Each test drives ``_rss_threshold_check`` over a list of readings, one tick
    per reading on an installed clock, and counts the resets.
    """

    CEILING = 4096
    PID = 4242
    #: Seconds between two ticks: the watchdog's interval, to the second.
    TICK_SECS = 300.0

    @pytest.fixture(autouse=True)
    def _on_the_proc_route(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(session.platform_compat, "IS_WINDOWS", False)

    def _watched(self, keys: tuple[str, ...] = ("dashboard:x",)):
        """A manager with idle sessions on one runtime, a clock and the notice seams."""
        manager = _make_manager(rss_max_mb=self.CEILING)
        for key in keys:
            manager._sessions[key] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=self.PID)
        manager.reset = AsyncMock(return_value=True)
        self.clock = [5000.0]
        self.cleanup = TestRssThresholdCheck._clocked(manager, self.clock)
        self.recycled = AsyncMock()
        manager.set_recycle_callback(self.recycled)
        self.held: list = []
        manager.on_rss_hold = lambda key, hold: self.held.append((key, hold))
        return manager

    async def _ticks(self, manager, readings: list[int]) -> None:
        """Run one tick per reading, ``TICK_SECS`` apart, the tree reading *readings* in turn."""
        rss = [0]
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", side_effect=lambda *_: rss[0]),
        ):
            for reading in readings:
                rss[0] = reading
                await manager._rss_threshold_check()
                self.clock[0] += self.TICK_SECS

    async def _two_readings(self, manager, first: int, gap: float, second: int) -> None:
        """Read the runtime at *first*, then *gap* seconds later at *second*."""
        await self._ticks(manager, [first])
        self.clock[0] += gap - self.TICK_SECS  # ``_ticks`` already moved it one tick on
        await self._ticks(manager, [second])

    def _footprint(self, pid: int = PID):
        return self.cleanup.state.rss_footprints.get(pid)

    @pytest.mark.asyncio
    async def test_a_runtime_found_over_the_ceiling_is_held_without_a_recycle(self, caplog) -> None:
        """A 64-server chat reads about 7 GB from its first tick. That is its size,
        so no tick recycles it, the chat gets no notice, and the log names the
        figure it is recycled past once."""
        from kiro_crew.session_rss_rebound import RssFootprint

        manager = self._watched()
        with caplog.at_level("INFO"):
            await self._ticks(manager, [7000] * 6)
        manager.reset.assert_not_awaited()
        assert self.held == []
        self.recycled.assert_not_awaited()
        assert self._footprint() == RssFootprint(5000.0, seen_mb=7000, held_mb=7000, settled=True)
        footprint_lines = [
            r.getMessage() for r in caplog.records if r.getMessage().startswith("RSS footprint")
        ]
        assert footprint_lines == [
            "RSS footprint: session dashboard:x runtime 4242 tree rss=7000MB is above "
            "4096MB on its first reading; its footprint is taken on the next tick",
            "RSS footprint: session dashboard:x runtime 4242 tree rss=7000MB settled "
            "above 4096MB without having been read under it; that is this workload's "
            "own size, so it is left running unless it passes 14000MB",
        ]

    @pytest.mark.parametrize(
        ("readings", "held_mb"),
        [
            # Read while its servers were still starting: 5.8 GB, then the steady 10.6.
            ([5772, 10600, 11423, 11423], 10600),
            # Read under the ceiling before its servers were up, then at its size.
            ([3000, 7000, 7600, 7000], 7000),
            # A start-up peak that settles lower is held at the lower figure.
            ([7600, 7000, 7000], 7000),
        ],
        ids=["mid-start-over", "mid-start-under", "start-peak"],
    )
    @pytest.mark.asyncio
    async def test_the_footprint_is_the_second_reading(self, readings, held_mb) -> None:
        manager = self._watched()
        await self._ticks(manager, readings)
        manager.reset.assert_not_awaited()
        footprint = self._footprint()
        assert (footprint.held_mb, footprint.settled) == (held_mb, True)

    @pytest.mark.asyncio
    async def test_a_held_runtime_is_recycled_once_it_passes_twice_its_footprint(self) -> None:
        manager = self._watched()
        await self._ticks(manager, [7000, 7000, 14000])  # exactly twice: still held
        manager.reset.assert_not_awaited()
        await self._ticks(manager, [14001])
        manager.reset.assert_awaited_once()
        assert self.recycled.await_args.kwargs["reason"] == "memory limit (14001MB)"
        assert self.held == []

    @pytest.mark.asyncio
    async def test_an_outgrown_hold_does_not_come_back_when_the_recycle_is_declined(self) -> None:
        """A guard declining the recycle leaves the process running. The next tick
        must try again at the ceiling, not re-hold it at its grown size."""
        manager = self._watched()
        manager.reset = AsyncMock(side_effect=[False, True])
        await self._ticks(manager, [7000, 7000, 14001, 9000])
        assert manager.reset.await_count == 2
        assert self._footprint().held_mb == 0

    @pytest.mark.parametrize(
        "readings",
        [[1500, 1500, 4097], [4500, 3300, 4097]],
        ids=["read-under-twice", "start-peak-then-under"],
    )
    @pytest.mark.asyncio
    async def test_a_runtime_that_settled_under_the_ceiling_is_recycled_on_its_first_crossing(
        self, readings
    ) -> None:
        manager = self._watched()
        await self._ticks(manager, readings[:-1])
        manager.reset.assert_not_awaited()
        await self._ticks(manager, readings[-1:])
        manager.reset.assert_awaited_once()
        assert self.recycled.await_args.kwargs["reason"] == "memory limit (4097MB)"

    @pytest.mark.asyncio
    async def test_exactly_the_ceiling_on_the_second_reading_is_not_a_hold(self) -> None:
        manager = self._watched()
        await self._ticks(manager, [4096, 4096])
        assert self._footprint().held_mb == 0
        await self._ticks(manager, [4097])
        manager.reset.assert_awaited_once()

    @pytest.mark.parametrize(
        ("readings", "settled_after"),
        [([0, 0, 7000, 7000], 4), ([1500, 0, 7000], 3)],
        ids=["unreadable-first", "unreadable-second"],
    )
    @pytest.mark.asyncio
    async def test_an_unreadable_tree_settles_nothing(self, readings, settled_after) -> None:
        """A tree that reads 0 was not read. Counting it as a reading under the
        ceiling would make the 7 GB that follows look like growth."""
        manager = self._watched()
        await self._ticks(manager, readings[: settled_after - 1])
        footprint = self._footprint()
        assert footprint is None or not footprint.settled
        await self._ticks(manager, readings[settled_after - 1 :])
        manager.reset.assert_not_awaited()
        assert self._footprint().held_mb == 7000

    @pytest.mark.asyncio
    async def test_sessions_sharing_a_runtime_move_its_footprint_once_per_tick(self) -> None:
        """Two sessions on one runtime are judged in one tick on one reading. The
        second must not count as the runtime's second reading."""
        manager = self._watched(keys=("dashboard:a", "dashboard:b"))
        await self._ticks(manager, [5772])
        assert self._footprint().settled is False
        await self._ticks(manager, [10600, 11423])
        manager.reset.assert_not_awaited()
        assert self._footprint().held_mb == 10600

    @pytest.mark.parametrize(
        ("first", "idle_again_after", "second", "recycles", "held_at_mb"),
        [
            # Read under the ceiling, then busy for an hour: the next idle reading is
            # growth the watchdog could not see, not the runtime's start-up size.
            (1500, 3600.0, 20000, 1, 0),
            # The gaps the loop really leaves: a tick starts one interval after the
            # last one ended, so readings come 302 s apart, not 300. The next tick.
            (3000, 302.0, 7000, 0, 7000),
            # Busy at one tick only: the reading after it is still start-up.
            (3000, 604.0, 7000, 0, 7000),
            # Busy at two ticks: read under the ceiling too long ago.
            (3000, 906.0, 7000, 1, 0),
            # Already over the ceiling when first read: held at the second reading
            # however late it comes. No reading tells its size by design from growth.
            (5000, 3600.0, 9000, 0, 9000),
        ],
        ids=[
            "under-then-an-hour",
            "under-then-the-next-tick",
            "under-then-one-busy-tick",
            "under-then-two-busy-ticks",
            "over-late",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_first_reading_under_the_ceiling_is_start_up_only_while_recent(
        self, first, idle_again_after, second, recycles, held_at_mb
    ) -> None:
        manager = self._watched()
        await self._two_readings(manager, first, idle_again_after, second)
        assert manager.reset.await_count == recycles
        assert self._footprint().held_mb == held_at_mb
        assert self.held == []

    @pytest.mark.parametrize("interval", [300.0, 100.0], ids=["as-shipped", "a-shorter-tick"])
    @pytest.mark.asyncio
    async def test_the_start_up_window_is_counted_in_the_watchdogs_own_ticks(
        self, interval
    ) -> None:
        """The bound is two and a half of the loop's longest tick, read from the
        loop's own constant, so it moves when that interval does."""
        from kiro_crew.session_cleanup import SessionCleanup
        from kiro_crew.session_rss_rebound import RSS_FOOTPRINT_SETTLE_TICKS

        window = RSS_FOOTPRINT_SETTLE_TICKS * interval
        with patch.object(SessionCleanup, "MAX_TICK_INTERVAL_SECS", interval):
            for gap, recycles in ((2 * interval, 0), (window, 0), (window + 1.0, 1)):
                manager = self._watched()
                await self._two_readings(manager, 3000, gap, 7000)
                assert manager.reset.await_count == recycles, gap

    @pytest.mark.asyncio
    async def test_the_hold_line_names_a_first_reading_under_the_ceiling(self, caplog) -> None:
        """A runtime first read under the ceiling WAS read under it, so its hold
        line says when and at what size instead."""
        manager = self._watched()
        with caplog.at_level("WARNING"):
            await self._two_readings(manager, 3000, 604.0, 7000)
        assert [r.getMessage() for r in caplog.records if "RSS footprint" in r.getMessage()] == [
            "RSS footprint: session dashboard:x runtime 4242 tree rss=7000MB settled "
            "above 4096MB 604s after a first reading of 3000MB, which counts as its "
            "start-up; that is this workload's own size, so it is left running unless "
            "it passes 14000MB"
        ]

    def _fresh_pid_after_each_recycle(self, manager) -> None:
        """Give *manager* what a recycle does: the session's next process is a new pid."""
        pids = [self.PID]

        async def reset(*_args, **_kwargs) -> bool:
            pids[0] += 1
            return True

        manager.reset = AsyncMock(side_effect=reset)
        manager.get_pid = MagicMock(side_effect=lambda _key: pids[0])

    @pytest.mark.asyncio
    async def test_the_fresh_process_after_a_recycle_takes_no_footprint(self) -> None:
        """A recycled session's next process has a new pid. Found straight back
        over the ceiling it is a rebound and is recycled, not held in silence."""
        manager = self._watched()
        self._fresh_pid_after_each_recycle(manager)
        await self._ticks(manager, [1500, 1500, 5000])
        manager.reset.assert_awaited_once()
        await self._ticks(manager, [5000])
        assert manager.reset.await_count == 2
        assert self.recycled.await_args.kwargs["reason"] == (
            "memory limit (5000MB), over session.watchdog_rss_max_mb of 4096MB "
            "again since its last recycle"
        )
        # Recorded as a runtime the ceiling judges, never as one held at its size.
        assert [f.held_mb for f in self.cleanup.state.rss_footprints.values()] == [0]
        assert self.held == []

    @pytest.mark.parametrize(
        ("readings", "recycles", "hold_notices", "held_at_mb"),
        [
            # A many-server chat the watchdog never recycled: held at its size, no notice.
            ([7000] * 6, 0, [], [7000]),
            # The same chat once it passed twice its size: recycled, and each fresh
            # process is then a rebound, so two more recycles and a hold with its notice.
            ([7000, 7000, 14001, 7000, 7000, 7000, 7000], 3, [3], []),
            # A small chat that grew over the ceiling and comes straight back over it.
            ([1500, 1500, 5000, 5000, 5000, 5000, 5000], 3, [3], []),
            # A first turn made the chat large: over the ceiling on both readings and
            # never recycled, so it is held at that size, with no notice.
            ([5000, 5000, 9000, 10000], 0, [], [5000]),
            # And stopped once it passes twice that size.
            ([5000, 5000, 10001], 1, [], []),
        ],
        ids=[
            "many-servers",
            "many-servers-after-a-recycle",
            "grew-and-came-back",
            "large-first-turn",
            "large-first-turn-past-twice",
        ],
    )
    @pytest.mark.asyncio
    async def test_which_chats_are_held_and_which_are_recycled(
        self, readings, recycles, hold_notices, held_at_mb
    ) -> None:
        """One tick per reading; a recycle starts a fresh process on the next tick.

        ``hold_notices`` is the recycle count each chat notice names: the footprint
        hold tells the chat nothing, the hold after three recycles in a row does.
        """
        manager = self._watched()
        self._fresh_pid_after_each_recycle(manager)
        await self._ticks(manager, readings)
        assert manager.reset.await_count == recycles
        assert [hold.recycles for _key, hold in self.held] == hold_notices
        footprints = self.cleanup.state.rss_footprints.values()
        assert [f.held_mb for f in footprints if f.held_mb] == held_at_mb

    @pytest.mark.asyncio
    async def test_a_runtime_first_read_under_a_recycle_record_never_takes_a_footprint(
        self,
    ) -> None:
        """Three recycles, then the hold with its notice. A day later the record
        ends and the same process is still over the ceiling: it is recycled, as it
        was before footprints existed, not held in silence at its grown size."""
        from kiro_crew.session_rss_rebound import RSS_REBOUND_WINDOW_SECS

        manager = self._watched()
        self._fresh_pid_after_each_recycle(manager)
        await self._ticks(manager, [1500, 1500, 5000, 5000, 5000, 5000, 9000])
        assert manager.reset.await_count == 3
        assert [hold.recycles for _key, hold in self.held] == [3]
        self.clock[0] += RSS_REBOUND_WINDOW_SECS
        await self._ticks(manager, [9000])
        assert manager.reset.await_count == 4
        assert self.recycled.await_args.kwargs["reason"] == "memory limit (9000MB)"
        assert [hold.recycles for _key, hold in self.held] == [3]


class TestRssFootprintAtTheRealTickSpacing(TestRssFootprint):
    """Every footprint case again with the clock stepped as the loop steps it.

    A tick starts one interval after the last one ended, so two readings are the
    interval plus the tick's own run time apart: 302 s measured on Linux. A bound
    that is a whole number of intervals passes at 300.0 and fails in the loop.
    """

    TICK_SECS = 302.0


class TestRssThresholdSpareRuntime:
    """The RSS tick measures the warm-pool spare runtime no session owns.

    The spare is pre-spawned at boot and parked in the pool until a session
    claims it, so it is absent from ``_sessions`` and the per-session pass never
    sees it. Its tree still counts against the same ceiling; a hit takes it out
    of the pool and tears it down through the pool's own discard path, and
    nothing a user sees is touched.

    The Linux reader is pinned so these exercise the shared
    ``session_cleanup_spare.build_rss_measure`` deterministically on every host;
    the Windows branch of that reader has its own coverage in the session-pass
    tests.
    """

    @pytest.fixture(autouse=True)
    def _linux_measure(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(session.platform_compat, "IS_WINDOWS", False)

    @pytest.mark.asyncio
    async def test_over_ceiling_spare_is_removed_and_discarded(self, caplog) -> None:
        # Fails on main: with no chat session in _sessions, the per-session pass
        # has nothing to measure and the spare's over-ceiling tree is ignored,
        # so it stays queued and _discard_pool_provider is never called.
        manager = _make_manager(rss_max_mb=1000)
        provider = _pool_warm_provider(manager, pid=7777)
        manager._discard_pool_provider = AsyncMock()
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
            caplog.at_level("WARNING"),
        ):
            await manager._rss_threshold_check()
        manager._discard_pool_provider.assert_awaited_once()
        assert manager._discard_pool_provider.await_args.args[0] is provider
        assert "spare runtime pid=7777 tree rss=2048MB exceeds 1000MB" in caplog.text
        # The sweep shield added for the removal is dropped once the process is
        # torn down, so it does not leak and hide a later reuse of that pid.
        assert 7777 not in manager._pool_sweep_pids

    @pytest.mark.asyncio
    async def test_recycle_schedules_a_refill(self) -> None:
        # After taking a spare out, the pool is one short; refill through the
        # same helper the health sweep uses so it is not left short until then.
        manager = _make_manager(rss_max_mb=1000)
        _pool_warm_provider(manager, pid=7777)
        manager._discard_pool_provider = AsyncMock()
        manager._schedule_replenish = MagicMock()
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        manager._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_recycled_spare_is_no_longer_claimable(self) -> None:
        # The recycled spare must leave the queue, or the next New chat claims a
        # dead runtime.
        manager = _make_manager(rss_max_mb=1000)
        _pool_warm_provider(manager, pid=7777)
        manager._discard_pool_provider = AsyncMock()
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        assert manager.warm_providers() == []

    @pytest.mark.asyncio
    async def test_spare_claimed_between_measure_and_discard_is_left_alone(self) -> None:
        # A session can claim the spare across the measurement await. The removal
        # is synchronous with its own membership check, so a spare absent from
        # the queue is not found and never torn down under its new session.
        manager = _make_manager(rss_max_mb=1000)
        provider = _pool_warm_provider(manager, pid=7777)
        manager._discard_pool_provider = AsyncMock()

        def _claim(pid, child_map):
            # The claim happens during the measurement: drain the spare out as a
            # real claimant would before the removal pass runs.
            manager._claim_from_pool(agent=None)
            return 2048

        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", side_effect=_claim),
        ):
            await manager._rss_threshold_check()
        manager._discard_pool_provider.assert_not_awaited()
        # The claimant holds it; the pool does not.
        assert provider not in manager.warm_providers()

    @pytest.mark.asyncio
    async def test_under_ceiling_spare_is_left_alone(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        _pool_warm_provider(manager, pid=7777)
        manager._discard_pool_provider = AsyncMock()
        manager._schedule_replenish = MagicMock()
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=500),
        ):
            await manager._rss_threshold_check()
        manager._discard_pool_provider.assert_not_awaited()
        # The snapshot does not consume the pool: the spare is still claimable.
        assert len(manager.warm_providers()) == 1
        # Nothing was removed, so no refill is scheduled.
        manager._schedule_replenish.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_ceiling_measures_no_spare(self) -> None:
        manager = _make_manager(rss_max_mb=0)
        _pool_warm_provider(manager, pid=7777)
        manager._discard_pool_provider = AsyncMock()
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=999999) as rt,
        ):
            await manager._rss_threshold_check()
        manager._discard_pool_provider.assert_not_awaited()
        rt.assert_not_called()
        assert len(manager.warm_providers()) == 1

    @pytest.mark.asyncio
    async def test_spare_measured_once_per_distinct_pid(self) -> None:
        # A fresh pool snapshot could list a reused entry twice; the tree is
        # read once per pid and the runtime discarded once.
        manager = _make_manager(rss_max_mb=1000)
        provider = _warm_provider(pid=7777)
        manager._spare_pid_providers = MagicMock(return_value=[(7777, provider), (7777, provider)])
        manager._remove_pooled_providers_by_pid = MagicMock(return_value=[(7777, provider)])
        manager._discard_pool_provider = AsyncMock()
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048) as rt,
        ):
            await manager._rss_threshold_check()
        assert rt.call_count == 1
        manager._discard_pool_provider.assert_awaited_once()

    @pytest.mark.usefixtures("rss_runtimes_read_under_ceiling")
    @pytest.mark.asyncio
    async def test_sessions_and_spare_both_recycle_in_one_tick(self) -> None:
        # The two passes are independent: an over-ceiling session resets and an
        # over-ceiling spare is discarded in the same tick.
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        spare = _pool_warm_provider(manager, pid=7777)
        manager._discard_pool_provider = AsyncMock()
        with (
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await manager._rss_threshold_check()
        manager.reset.assert_awaited_once()
        assert manager.reset.await_args.args[0] == "dashboard:x"
        manager._discard_pool_provider.assert_awaited_once()
        assert manager._discard_pool_provider.await_args.args[0] is spare


class TestResetGuards:
    """reset()'s atomic identity + not-busy guards used by the RSS watchdog."""

    @pytest.mark.asyncio
    async def test_reset_no_op_when_occupant_object_changed(self) -> None:
        # A session reset+recreated under the same key between the off-lock RSS
        # measurement and reset() must NOT be recycled on the prior occupant's
        # stale reading.
        manager = _make_manager(rss_max_mb=1000)
        current = _session_stub(busy=False)
        manager._sessions["dashboard:x"] = current
        sampled_earlier = _session_stub(busy=False)  # different object
        recycled = await manager.reset("dashboard:x", expect_session=sampled_earlier)
        assert recycled is False
        assert manager._sessions.get("dashboard:x") is current  # untouched

    @pytest.mark.asyncio
    async def test_reset_no_op_when_busy_and_skip_flag_set(self) -> None:
        # A turn acquiring the semaphore before reset() takes the lock must not
        # be cut mid-stream.
        manager = _make_manager(rss_max_mb=1000)
        busy = _session_stub(busy=True)
        manager._sessions["dashboard:x"] = busy
        recycled = await manager.reset("dashboard:x", skip_if_busy=True)
        assert recycled is False
        assert manager._sessions.get("dashboard:x") is busy  # untouched
