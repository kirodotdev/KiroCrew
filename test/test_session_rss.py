"""Tests for the RSS-threshold recycle hook (CR 1, new behaviour).

Covers:
  * get_session_rss_mb / _rss_mb_from_tree tree accumulation + subtree barrier
  * the REAL /proc parsing primitives (_read_rss_pages, _build_child_map),
    exercised against a fake /proc tree rather than mocked away
  * per-PID sub-MiB accumulation (pages summed, truncated to MiB once)
  * _rss_threshold_check: disabled by default, busy-skip, threshold trigger,
    persistent- and channel-key protection, the collect->reset race guard,
    gating of the recycle notification on an actual reset, and a single
    per-tick /proc child-map scan (not one scan per candidate)
  * reset()'s atomic identity (expect_session) + not-busy (skip_if_busy) guards
"""

from __future__ import annotations

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


def _mib_of_pages(total_pages: int) -> int:
    return (total_pages * session_pid._PAGE_SIZE) // (1024 * 1024)


class TestGetSessionRssMb:
    @pytest.fixture(autouse=True)
    def _off_windows_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default this class to the /proc route.

        The tree-accumulation tests below set ``sys.platform`` to "linux" and stub
        the /proc primitives, but the Windows dispatch is keyed off
        ``platform_compat.IS_WINDOWS``, which a Windows host reports as True — so
        without this they would take the Win32 route and never reach the stubs.
        The Windows tests re-patch it to True explicitly.
        """
        monkeypatch.setattr(session_pid.platform_compat, "IS_WINDOWS", False)

    def test_macos_returns_zero(self) -> None:
        """macOS has no ctypes-only per-pid RSS route, so the ceiling stays inert."""
        with patch("kiro_crew.session_pid.sys.platform", "darwin"), patch(
            "kiro_crew.session_pid.platform_compat.IS_WINDOWS", False
        ):
            assert session_pid.get_session_rss_mb(123) == 0

    def test_windows_measures_the_tree_rather_than_returning_zero(self) -> None:
        """Windows has no /proc, but it MUST still measure.

        Returning 0 there made the configured ``watchdog_rss_max_mb`` ceiling
        unreachable, so a session tree could grow without ever being recycled.
        """
        with patch("kiro_crew.session_pid.sys.platform", "win32"), patch(
            "kiro_crew.session_pid.platform_compat.IS_WINDOWS", True
        ), patch(
            "kiro_crew.session_pid.platform_compat.proc_rss_tree_mb_for_pid",
            return_value=512.7,
        ):
            assert session_pid.get_session_rss_mb(100) == 512

    def test_windows_uses_the_lineage_validated_helper_not_a_raw_walk(self) -> None:
        """Toolhelp's PPID field is never cleared when a parent exits and Windows
        recycles PIDs, so a raw parent->child walk can attach an unrelated subtree
        to a recycled PID -- and recycle a HEALTHY session. The measurement must
        go through the helper that validates each edge against creation times."""
        with patch("kiro_crew.session_pid.sys.platform", "win32"), patch(
            "kiro_crew.session_pid.platform_compat.IS_WINDOWS", True
        ), patch(
            "kiro_crew.session_pid.platform_compat.proc_rss_tree_mb_for_pid",
            return_value=1.0,
        ), patch(
            "kiro_crew.session_pid._build_child_map",
            side_effect=AssertionError("must not walk a raw Toolhelp parent map"),
        ):
            assert session_pid.get_session_rss_mb(100) == 1

    def test_windows_treats_an_unreadable_tree_as_zero(self) -> None:
        """None means "unknown"; the ceiling must not fire on a guess."""
        with patch("kiro_crew.session_pid.sys.platform", "win32"), patch(
            "kiro_crew.session_pid.platform_compat.IS_WINDOWS", True
        ), patch(
            "kiro_crew.session_pid.platform_compat.proc_rss_tree_mb_for_pid",
            return_value=None,
        ):
            assert session_pid.get_session_rss_mb(100) == 0

    def test_accumulates_root_plus_descendants(self) -> None:
        # tree: 100 -> [200, 300]; 300 -> [400]
        child_map = {100: [200, 300], 300: [400]}
        pages = {100: 1000, 200: 2000, 300: 3000, 400: 4000}
        with patch("kiro_crew.session_pid.sys.platform", "linux"), patch(
            "kiro_crew.session_pid._build_child_map", return_value=child_map
        ), patch(
            "kiro_crew.session_pid._read_rss_pages",
            side_effect=lambda p, proc_root=None: pages.get(p, 0),
        ):
            assert session_pid.get_session_rss_mb(100) == _mib_of_pages(10000)

    def test_exclude_pids_prunes_subtree(self) -> None:
        # Excluding 300 must also drop its child 400.
        child_map = {100: [200, 300], 300: [400]}
        pages = {100: 1000, 200: 2000, 300: 3000, 400: 4000}
        with patch("kiro_crew.session_pid.sys.platform", "linux"), patch(
            "kiro_crew.session_pid._build_child_map", return_value=child_map
        ), patch(
            "kiro_crew.session_pid._read_rss_pages",
            side_effect=lambda p, proc_root=None: pages.get(p, 0),
        ):
            assert session_pid.get_session_rss_mb(100, exclude_pids={300}) == _mib_of_pages(3000)

    def test_cycle_safe(self) -> None:
        # Pathological cycle 100 -> 200 -> 100 must terminate; each counted once.
        child_map = {100: [200], 200: [100]}
        with patch("kiro_crew.session_pid.sys.platform", "linux"), patch(
            "kiro_crew.session_pid._build_child_map", return_value=child_map
        ), patch(
            "kiro_crew.session_pid._read_rss_pages",
            side_effect=lambda p, proc_root=None: 5,
        ):
            assert session_pid.get_session_rss_mb(100) == _mib_of_pages(10)

    def test_sub_mib_per_pid_not_truncated_away(self) -> None:
        # Regression for the per-PID MiB-truncation bug: two sibling processes
        # each just over half a MiB. A per-PID ``// MiB`` truncates each to 0
        # (the old behaviour reported 0 for the tree); summing pages first and
        # truncating once yields >= 1 MiB.
        pages_per_mib = (1024 * 1024) // session_pid._PAGE_SIZE
        half = pages_per_mib // 2 + 1  # just over 0.5 MiB each
        # Precondition: each PID individually truncates to 0 MiB.
        assert (half * session_pid._PAGE_SIZE) // (1024 * 1024) == 0
        child_map = {100: [200]}
        pages = {100: half, 200: half}
        with patch("kiro_crew.session_pid.sys.platform", "linux"), patch(
            "kiro_crew.session_pid._build_child_map", return_value=child_map
        ), patch(
            "kiro_crew.session_pid._read_rss_pages",
            side_effect=lambda p, proc_root=None: pages.get(p, 0),
        ):
            assert session_pid.get_session_rss_mb(100) >= 1

    def test_rss_mb_from_tree_uses_shared_map(self) -> None:
        # _rss_mb_from_tree consumes a prebuilt map (no /proc scan of its own).
        child_map = {100: [200, 300], 300: [400]}
        pages = {100: 1000, 200: 2000, 300: 3000, 400: 4000}
        with patch(
            "kiro_crew.session_pid._read_rss_pages",
            side_effect=lambda p, proc_root=None: pages.get(p, 0),
        ), patch("kiro_crew.session_pid._build_child_map") as bm:
            got = session_pid._rss_mb_from_tree(100, child_map)
        assert got == _mib_of_pages(10000)
        bm.assert_not_called()  # never scans /proc itself


class TestProcParsingPrimitives:
    """Exercise the REAL /proc parsing (previously fully mocked) against a fake
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

    The RSS ceiling was a silent no-op on Windows (every tree measured 0 MiB, so
    an over-budget tree was never recycled). The fix routes whole-tree
    measurement through a lineage-validating helper rather than teaching these
    two primitives a Win32 dialect -- pairing a raw Toolhelp parent map with an
    aggressively recycled PID would sum an unrelated subtree and recycle a
    HEALTHY session, which is worse than not recycling at all.
    """

    def test_build_child_map_has_no_windows_branch_by_design(self) -> None:
        """A raw Toolhelp parent map is deliberately NOT used here.

        ``th32ParentProcessID`` is never cleared when a parent exits, so pairing
        it with an aggressively recycled PID would sum an unrelated subtree and
        recycle a healthy session. Windows gets its own lineage-validated route
        in ``get_session_rss_mb`` instead of a shareable snapshot.
        """
        import inspect

        assert "_windows_process_parent_map" not in inspect.getsource(
            session_pid._build_child_map
        )


class TestRssThresholdCheck:
    @pytest.fixture(autouse=True)
    def _on_the_proc_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the hook's /proc branch for the behaviour tests below.

        They assert on the sweep's DECISIONS (which sessions get recycled, and
        which are protected), so they stub the /proc measurement helpers. That
        stub only takes effect on the /proc branch, so a Windows host would
        otherwise measure real trees here and every one of them would read as
        under-threshold. The Windows dispatch has its own test above.
        """
        monkeypatch.setattr(session.platform_compat, "IS_WINDOWS", False)

    @pytest.mark.asyncio
    async def test_disabled_by_default_is_noop(self) -> None:
        manager = _make_manager(rss_max_mb=0)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock()
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=99999
        ) as g:
            await manager._rss_threshold_check()
        g.assert_not_called()
        manager.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_over_threshold_session_is_reset(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        stub = _session_stub(busy=False)
        manager._sessions["dashboard:x"] = stub
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=2048
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
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        with patch("kiro_crew.session._build_child_map", return_value={}) as bm, patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=2048
        ) as rt:
            await manager._rss_threshold_check()
        assert bm.call_count == 1  # one /proc scan for the whole tick
        assert rt.call_count == 2  # measured once per candidate

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
        manager.get_pid = MagicMock(return_value=4242)
        # Overrides the class fixture's /proc pin: this is the Windows branch.
        with patch.object(session.platform_compat, "IS_WINDOWS", True), patch(
            "kiro_crew.session._build_child_map",
            side_effect=AssertionError("must not build a raw Toolhelp parent map"),
        ), patch("kiro_crew.session.get_session_rss_mb", return_value=2048) as gs:
            await manager._rss_threshold_check()
        assert gs.call_count == 2
        assert manager.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_one_failed_victim_does_not_skip_the_rest(self) -> None:
        # reset() raising for the first victim must not prevent the second from
        # being recycled — the per-victim guard isolates failures.
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:a"] = _session_stub(busy=False)
        manager._sessions["dashboard:b"] = _session_stub(busy=False)
        manager.get_pid = MagicMock(return_value=4242)
        reset_calls: list[str] = []

        async def _reset(key, *, expect_session=None, skip_if_busy=False):
            reset_calls.append(key)
            if key == "dashboard:a":
                raise RuntimeError("boom")
            return True

        manager.reset = _reset  # type: ignore[assignment]
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=2048
        ):
            await manager._rss_threshold_check()  # must not raise
        assert reset_calls == ["dashboard:a", "dashboard:b"]

    @pytest.mark.asyncio
    async def test_recycle_fires_notification_callback(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        cb = AsyncMock()
        manager.set_recycle_callback(cb)
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=2048
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

        async def _reset(key, *, expect_session=None, skip_if_busy=False):
            reset_calls.append(key)
            return key == "dashboard:b"  # 'a' is a no-op, 'b' recycled

        manager.reset = _reset  # type: ignore[assignment]
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=2048
        ):
            await manager._rss_threshold_check()
        assert set(reset_calls) == {"dashboard:a", "dashboard:b"}
        cb.assert_awaited_once()
        assert cb.await_args.args[0] == "dashboard:b"

    @pytest.mark.asyncio
    async def test_no_notification_when_under_threshold(self) -> None:
        manager = _make_manager(rss_max_mb=4096)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        cb = AsyncMock()
        manager.set_recycle_callback(cb)
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=512
        ):
            await manager._rss_threshold_check()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_under_threshold_session_is_kept(self) -> None:
        manager = _make_manager(rss_max_mb=4096)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=512
        ):
            await manager._rss_threshold_check()
        manager.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_busy_session_is_skipped(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=True)  # turn in flight
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=999999
        ) as g:
            await manager._rss_threshold_check()
        g.assert_not_called()  # busy session never measured
        manager.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persistent_key_is_protected(self) -> None:
        from kiro_crew.session import _PERSISTENT_KEYS

        manager = _make_manager(rss_max_mb=1000)
        pkey = next(iter(_PERSISTENT_KEYS))
        manager._sessions[pkey] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=999999
        ) as g:
            await manager._rss_threshold_check()
        g.assert_not_called()
        manager.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_key_is_protected(self) -> None:
        # Channel sessions are protected from idle expiry; RSS recycle must
        # skip them too and never even measure them.
        from kiro_crew.session import _CHANNEL_PREFIX

        manager = _make_manager(rss_max_mb=1000)
        ckey = f"{_CHANNEL_PREFIX}team-eng"
        manager._sessions[ckey] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=999999
        ) as g:
            await manager._rss_threshold_check()
        g.assert_not_called()
        manager.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_pid_is_skipped(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_stub(busy=False)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=None)  # provider has no pid
        with patch("kiro_crew.session._build_child_map", return_value={}), patch(
            "kiro_crew.session._rss_mb_from_tree", return_value=999999
        ) as g:
            await manager._rss_threshold_check()
        g.assert_not_called()
        manager.reset.assert_not_awaited()


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
