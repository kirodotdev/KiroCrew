"""Unreachable-gatewayd reclamation (issue #3315).

Two layers under test:

* ``gatewayd._socket_liveness_sweeper`` — the daemon self-exits when its own
  listening socket path disappears (three consecutive ENOENT observations),
  and never exits on inconclusive stat failures or non-consecutive misses.
* ``session_pid._is_sweepable_orphan_gatewayd`` + ``_kill_orphan_gatewayd`` —
  the untracked orphan sweep reaps a gatewayd whose ``--socket`` path is gone
  from disk, TERM-first, while ``kiro_crew.cli`` / ``kiro_crew.__main__`` and
  live-socket daemons stay excluded.

The ``run_gatewayd`` tests bind a real local socket under ``tmp_path``; no
subprocesses are spawned and no sandbox is touched.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import session_pid as sp
from kiro_crew.mcp_gateway import gatewayd as gw

# POSIX process/socket semantics: ``os.killpg`` does not exist on Windows
# (even patching it fails with AttributeError), and the daemon's endpoint
# there is a named pipe with no directory entry to observe or unlink.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX socket-file and killpg semantics"
)

# ── gatewayd._socket_liveness_sweeper ──────────────────────────────


class TestSocketLivenessSweeper:
    @pytest.mark.asyncio
    async def test_exits_after_consecutive_misses(self, tmp_path: Path) -> None:
        """Socket unlinked -> three consecutive misses -> stop_event set."""
        sock = tmp_path / "gw.sock"
        sock.write_text("")  # stand-in for the bound endpoint
        stop = asyncio.Event()
        task = asyncio.create_task(gw._socket_liveness_sweeper(sock, 0.01, stop))
        try:
            await asyncio.sleep(0.05)
            assert not stop.is_set(), "must not exit while the path exists"
            sock.unlink()
            await asyncio.wait_for(stop.wait(), timeout=5)
            # stop_event fired by the sweeper itself — the task finishes
            # cleanly on its own.
            await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()

    @pytest.mark.asyncio
    async def test_does_not_exit_while_path_exists(self, tmp_path: Path) -> None:
        sock = tmp_path / "gw.sock"
        sock.write_text("")
        stop = asyncio.Event()
        task = asyncio.create_task(gw._socket_liveness_sweeper(sock, 0.01, stop))
        await asyncio.sleep(0.2)  # ~20 intervals, all hits
        assert not stop.is_set()
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    @pytest.mark.asyncio
    async def test_inconclusive_stat_failure_never_triggers_exit(
        self, tmp_path: Path
    ) -> None:
        """EACCES-style failures are not misses — the daemon must stay up."""
        sock = tmp_path / "gw.sock"
        sock.write_text("")
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if str(path) == str(sock):
                raise PermissionError(13, "denied", str(sock))
            return real_stat(path, *args, **kwargs)

        stop = asyncio.Event()
        with patch("kiro_crew.mcp_gateway.gatewayd.os.stat", side_effect=denied):
            task = asyncio.create_task(gw._socket_liveness_sweeper(sock, 0.01, stop))
            await asyncio.sleep(0.2)  # far more than 3 intervals of EACCES
            assert not stop.is_set()
            stop.set()
            await asyncio.wait_for(task, timeout=5)

    @pytest.mark.asyncio
    async def test_non_consecutive_misses_never_trigger_exit(
        self, tmp_path: Path
    ) -> None:
        """Misses interleaved with hits must never exit.

        Mutation guard for the CONSECUTIVE requirement: both removing the
        ``misses = 0`` reset (cumulative counting) and lowering the threshold
        (exit on any miss) make this pattern — miss, miss, hit, repeated —
        set ``stop_event``, failing the assertion.
        """
        sock = tmp_path / "gw.sock"
        sock.write_text("")
        real_stat = os.stat
        calls = {"n": 0}

        def flaky(path, *args, **kwargs):
            if str(path) == str(sock):
                calls["n"] += 1
                if calls["n"] % 3 != 0:  # two misses, then one hit, repeating
                    raise FileNotFoundError(2, "gone", str(sock))
            return real_stat(path, *args, **kwargs)

        stop = asyncio.Event()
        with patch("kiro_crew.mcp_gateway.gatewayd.os.stat", side_effect=flaky):
            task = asyncio.create_task(gw._socket_liveness_sweeper(sock, 0.01, stop))
            # Wait for enough probes that a cumulative counter would have
            # fired many times over.
            for _ in range(500):
                if calls["n"] >= 12:
                    break
                await asyncio.sleep(0.01)
            assert calls["n"] >= 12, "sweeper did not probe enough to prove anything"
            assert not stop.is_set()
            stop.set()
            await asyncio.wait_for(task, timeout=5)

    def test_armed_only_after_bind(self) -> None:
        """Structural ratchet: the liveness task is created after the bind.

        Before ``transport.serve`` returns, an absent socket path is a
        startup race, not unreachability — so the create_task for the
        liveness sweeper must appear AFTER the serve call in
        ``run_gatewayd``'s body.
        """
        import inspect

        src = inspect.getsource(gw.run_gatewayd)
        bind_at = src.index("transport.serve(")
        armed_at = src.index("_socket_liveness_sweeper(")
        assert bind_at < armed_at


@_POSIX_ONLY
class TestRunGatewaydSelfExit:
    @pytest.mark.asyncio
    async def test_daemon_self_exits_when_socket_is_unlinked(
        self, tmp_path: Path
    ) -> None:
        """End to end: bind a real endpoint, unlink it, daemon exits cleanly."""
        socket_path = tmp_path / "gw-selfexit.sock"
        stop_event = asyncio.Event()

        def _resolver(pool_key):  # never invoked — no stub connects
            raise AssertionError("no backend should be spawned")

        daemon = asyncio.create_task(
            gw.run_gatewayd(
                socket_path,
                max_backends=1,
                idle_timeout_secs=1,  # sweep interval clamps to 0.5s
                stop_event=stop_event,
                target_resolver=_resolver,
            )
        )
        try:
            for _ in range(500):
                if socket_path.exists():
                    break
                await asyncio.sleep(0.02)
            assert socket_path.exists(), "the daemon never bound its endpoint"
            socket_path.unlink()
            # 3 consecutive misses at 0.5s intervals ≈ 1.5–2s, then the
            # graceful drain. Generous ceiling for loaded CI hosts.
            await asyncio.wait_for(daemon, timeout=30)
            assert stop_event.is_set()
        finally:
            if not daemon.done():
                stop_event.set()
                await asyncio.wait_for(daemon, timeout=15)

    @pytest.mark.asyncio
    async def test_windows_never_arms_the_liveness_sweeper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With IS_WINDOWS=True the sweeper is not created: unlinking the
        endpoint path must NOT terminate the daemon (named pipes have no
        directory entry, so the probe would be meaningless there)."""
        monkeypatch.setattr(gw, "IS_WINDOWS", True)
        socket_path = tmp_path / "gw-win.sock"
        stop_event = asyncio.Event()

        def _resolver(pool_key):
            raise AssertionError("no backend should be spawned")

        daemon = asyncio.create_task(
            gw.run_gatewayd(
                socket_path,
                max_backends=1,
                idle_timeout_secs=1,
                stop_event=stop_event,
                target_resolver=_resolver,
            )
        )
        try:
            for _ in range(500):
                if socket_path.exists():
                    break
                await asyncio.sleep(0.02)
            assert socket_path.exists(), "the daemon never bound its endpoint"
            socket_path.unlink()
            await asyncio.sleep(2.5)  # > 3 sweep intervals + margin
            assert not daemon.done(), "daemon must keep serving without the probe"
            assert not stop_event.is_set()
        finally:
            stop_event.set()
            await asyncio.wait_for(daemon, timeout=15)


# ── session_pid gatewayd sweep predicate ───────────────────────────


def _gatewayd_cmdline(sock: Path | str, socket_form: str = "pair") -> bytes:
    parts = [b"/usr/bin/python3", b"-m", b"kiro_crew.mcp_gateway.gatewayd"]
    if socket_form == "pair":
        parts += [b"--socket", os.fsencode(str(sock))]
    elif socket_form == "equals":
        parts += [b"--socket=" + os.fsencode(str(sock))]
    parts += [b"--idle-timeout-secs", b"300", b"--max-backends", b"20"]
    return b"\x00".join(parts)


class TestIsSweepableOrphanGatewayd:
    def test_gatewayd_with_gone_socket_is_sweepable(self, tmp_path: Path) -> None:
        gone = tmp_path / "vanished.sock"
        assert sp._is_sweepable_orphan_gatewayd(_gatewayd_cmdline(gone)) is True

    def test_gatewayd_with_live_socket_is_not_sweepable(self, tmp_path: Path) -> None:
        live = tmp_path / "live.sock"
        live.write_text("")
        assert sp._is_sweepable_orphan_gatewayd(_gatewayd_cmdline(live)) is False

    def test_socket_equals_form_is_recognized(self, tmp_path: Path) -> None:
        gone = tmp_path / "vanished.sock"
        cmdline = _gatewayd_cmdline(gone, socket_form="equals")
        assert sp._is_sweepable_orphan_gatewayd(cmdline) is True

    def test_gatewayd_without_socket_arg_fails_closed(self) -> None:
        cmdline = _gatewayd_cmdline("", socket_form="none")
        assert sp._is_sweepable_orphan_gatewayd(cmdline) is False

    def test_relative_socket_path_fails_closed(self) -> None:
        """A relative --socket would be resolved against the SWEEPER's cwd,
        not the daemon's — a reachable daemon bound to gw.sock elsewhere
        would read as ENOENT here and be wrongly killed. Must never match."""
        for form in ("pair", "equals"):
            cmdline = _gatewayd_cmdline("gw.sock", socket_form=form)
            assert sp._is_sweepable_orphan_gatewayd(cmdline) is False
        # Control: the same name as an absolute path (nonexistent) matches.
        cmdline = _gatewayd_cmdline("/nonexistent-dir-3315/gw.sock")
        assert sp._is_sweepable_orphan_gatewayd(cmdline) is True

    def test_duplicate_socket_flags_use_the_last_occurrence(
        self, tmp_path: Path
    ) -> None:
        """argparse binds last-wins, so the LAST --socket is the path the
        daemon actually created. Statting the first would terminate a
        reachable daemon whose live socket is named later in argv."""
        live = tmp_path / "live.sock"
        live.write_text("")
        gone = tmp_path / "gone.sock"
        base = [b"/usr/bin/python3", b"-m", b"kiro_crew.mcp_gateway.gatewayd"]
        # gone first, live last -> daemon is reachable -> NOT sweepable.
        cmdline = b"\x00".join(
            base
            + [b"--socket", os.fsencode(str(gone)), b"--socket", os.fsencode(str(live))]
        )
        assert sp._is_sweepable_orphan_gatewayd(cmdline) is False
        # live first, gone last -> daemon bound the gone path -> sweepable.
        cmdline = b"\x00".join(
            base
            + [b"--socket", os.fsencode(str(live)), b"--socket", os.fsencode(str(gone))]
        )
        assert sp._is_sweepable_orphan_gatewayd(cmdline) is True
        # last occurrence relative -> unresolvable -> fail closed.
        cmdline = b"\x00".join(
            base + [b"--socket", os.fsencode(str(gone)), b"--socket", b"gw.sock"]
        )
        assert sp._is_sweepable_orphan_gatewayd(cmdline) is False

    def test_cli_and_main_entrypoints_are_never_sweepable(self, tmp_path: Path) -> None:
        gone = tmp_path / "vanished.sock"
        for module in (b"kiro_crew.cli", b"kiro_crew.__main__"):
            cmdline = b"\x00".join(
                [b"/usr/bin/python3", b"-m", module, b"--socket", os.fsencode(str(gone))]
            )
            assert sp._is_sweepable_orphan_gatewayd(cmdline) is False

    def test_space_joined_ps_cmdline_fails_closed(self, tmp_path: Path) -> None:
        """The macOS ps fallback cannot delimit paths — must never match."""
        gone = tmp_path / "vanished.sock"
        cmdline = _gatewayd_cmdline(gone).replace(b"\x00", b" ")
        assert sp._is_sweepable_orphan_gatewayd(cmdline) is False

    def test_marker_as_path_fragment_does_not_match(self, tmp_path: Path) -> None:
        """`vim kiro_crew.mcp_gateway.gatewayd` shapes are structural misses."""
        gone = tmp_path / "vanished.sock"
        cmdline = b"\x00".join(
            [
                b"vim",
                b"kiro_crew.mcp_gateway.gatewayd",
                b"--socket",
                os.fsencode(str(gone)),
            ]
        )
        assert sp._is_sweepable_orphan_gatewayd(cmdline) is False

    def test_inconclusive_stat_failure_fails_closed(self, tmp_path: Path) -> None:
        sock = tmp_path / "denied.sock"
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if str(path) == str(sock):
                raise PermissionError(13, "denied", str(sock))
            return real_stat(path, *args, **kwargs)

        with patch("kiro_crew.session_pid.os.stat", side_effect=denied):
            assert sp._is_sweepable_orphan_gatewayd(_gatewayd_cmdline(sock)) is False


class TestFindOrphanGatewaydCandidates:
    def test_gone_socket_gatewayd_becomes_a_candidate(self, tmp_path: Path) -> None:
        """The _GATEWAY_MARKERS exclusion is overridden by the reachability path."""
        gone = tmp_path / "vanished.sock"
        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[700]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_gatewayd_cmdline(gone)),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = sp.find_orphan_mcp_candidates(active_pids=set())
        assert result == [700]

    def test_live_socket_gatewayd_is_excluded(self, tmp_path: Path) -> None:
        live = tmp_path / "live.sock"
        live.write_text("")
        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[701]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_gatewayd_cmdline(live)),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = sp.find_orphan_mcp_candidates(active_pids=set())
        assert result == []

    def test_age_floor_still_applies(self, tmp_path: Path) -> None:
        gone = tmp_path / "vanished.sock"
        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[702]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_gatewayd_cmdline(gone)),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=10.0),
        ):
            mock_sys.platform = "linux"
            result = sp.find_orphan_mcp_candidates(active_pids=set())
        assert result == []


class TestKillOrphanGatewayd:
    def test_term_grace_covers_the_daemon_shutdown_budget(self) -> None:
        """The escalation grace must never truncate gatewayd's own graceful
        shutdown: an inverted pair (grace < drain budget) would SIGKILL a
        correctly-draining daemon mid-drain, losing in-flight responses and
        orphaning pooled backends — the exact failure TERM-first exists to
        avoid. Derivation from the shared budget makes the inversion
        unrepresentable; this pins it."""
        from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS

        assert sp._GATEWAYD_TERM_GRACE_SECONDS >= TOTAL_SHUTDOWN_BUDGET_SECS

    @_POSIX_ONLY
    def test_term_suffices_when_the_daemon_exits(self, tmp_path: Path) -> None:
        """SIGTERM delivered, process gone on first liveness poll -> 1 kill,
        no SIGKILL escalation."""
        gone = tmp_path / "vanished.sock"
        cmdline = _gatewayd_cmdline(gone)
        sent: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            sent.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # already exited
            if sig == signal.SIGKILL:
                raise AssertionError("must not escalate when TERM worked")

        with (
            patch("kiro_crew.session_pid.os.kill", side_effect=fake_kill),
            patch("kiro_crew.session_pid.os.killpg") as killpg,
        ):
            assert sp._kill_orphan_gatewayd(900, cmdline) == 1
        assert (900, signal.SIGTERM) in sent
        killpg.assert_not_called()

    @_POSIX_ONLY
    def test_wedged_daemon_is_escalated_to_killpg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gone = tmp_path / "vanished.sock"
        cmdline = _gatewayd_cmdline(gone)
        monkeypatch.setattr(sp, "_GATEWAYD_TERM_GRACE_SECONDS", 0.2)

        def fake_kill(pid: int, sig: int) -> None:
            return None  # alive forever, TERM ignored

        with (
            patch("kiro_crew.session_pid.os.kill", side_effect=fake_kill),
            patch("kiro_crew.session_pid.os.killpg") as killpg,
            patch("kiro_crew.session_pid.os.getpgid", return_value=901),
            patch("kiro_crew.session_pid.os.getpgrp", return_value=1234),
            patch.object(Path, "read_bytes", return_value=cmdline),
            patch("kiro_crew.session_pid.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            assert sp._kill_orphan_gatewayd(901, cmdline) == 1
        killpg.assert_called_once_with(901, signal.SIGKILL)

    @_POSIX_ONLY
    def test_recycled_pid_is_never_sigkilled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cmdline changed between TERM and escalation -> no force-kill."""
        gone = tmp_path / "vanished.sock"
        cmdline = _gatewayd_cmdline(gone)
        monkeypatch.setattr(sp, "_GATEWAYD_TERM_GRACE_SECONDS", 0.2)

        def fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                raise AssertionError("recycled PID must not be SIGKILLed")
            return None

        with (
            patch("kiro_crew.session_pid.os.kill", side_effect=fake_kill),
            patch("kiro_crew.session_pid.os.killpg") as killpg,
            patch.object(Path, "read_bytes", return_value=b"some\x00other\x00proc"),
            patch("kiro_crew.session_pid.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            sp._kill_orphan_gatewayd(902, cmdline)
        killpg.assert_not_called()

    @_POSIX_ONLY
    def test_kill_orphan_mcps_routes_gatewayd_through_term_first(
        self, tmp_path: Path
    ) -> None:
        """End to end through kill_orphan_mcps: fresh-cmdline re-verify picks
        the gatewayd branch and TERMs (not SIGKILLs) the daemon."""
        gone = tmp_path / "vanished.sock"
        cmdline = _gatewayd_cmdline(gone)
        sent: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            sent.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError

        with (
            patch("kiro_crew.session_pid.platform_compat") as pc,
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=cmdline),
            patch("kiro_crew.session_pid.os.kill", side_effect=fake_kill),
            patch("kiro_crew.session_pid.os.killpg") as killpg,
            patch("kiro_crew.session_pid.os.getpgrp", return_value=1234),
            patch("kiro_crew.session_pid.os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            pc.IS_WINDOWS = False
            mock_sys.platform = "linux"
            killed = sp.kill_orphan_mcps([903])
        assert killed == 1
        assert (903, signal.SIGTERM) in sent
        assert (903, signal.SIGKILL) not in sent
        killpg.assert_not_called()

    @_POSIX_ONLY
    def test_kill_phase_age_floor_protects_a_recycled_pid(
        self, tmp_path: Path
    ) -> None:
        """A candidate that exits after the find phase can have its PID
        recycled by a brand-new gatewayd that has not bound its socket yet.
        The kill-phase age recheck must skip such a young process — without
        it, the pre-bind daemon reads as 'socket absent' and gets TERMed."""
        gone = tmp_path / "vanished.sock"
        cmdline = _gatewayd_cmdline(gone)

        with (
            patch("kiro_crew.session_pid.platform_compat") as pc,
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=cmdline),
            patch("kiro_crew.session_pid.os.kill") as kill,
            patch("kiro_crew.session_pid.os.killpg") as killpg,
            patch("kiro_crew.session_pid.os.getpgrp", return_value=1234),
            patch("kiro_crew.session_pid.os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3.0),
        ):
            pc.IS_WINDOWS = False
            mock_sys.platform = "linux"
            killed = sp.kill_orphan_mcps([904])
        assert killed == 0
        kill.assert_not_called()
        killpg.assert_not_called()
