"""Tests for the MCP-launcher orphan SUBTREE reap.

Regression cover for a leak where 112 processes (15.2 GB RSS) survived 23 days
of sweeps on one host. The sweep reclaimed the marked launcher at the top of an
orphaned tree and relied on surviving children reparenting to init to become
candidates themselves on a later pass. That fallback breaks on an UNMARKED
intermediate: it is a candidate but not sweepable, so it lives forever AND hides
its own marked children behind a ppid that is not init, where
``_our_orphan_pids`` never enumerates them.

Observed shape, produced by any launcher wrapper that resolves a package and
then execs the resolved binary::

    <wrapper> mcp start-server <pkg>      <- marked, swept
      -> <wrapper> mcp start-server ...   <- marked, swept
        -> node .../bin/<pkg>-server      <- UNMARKED, leaked
          -> npm exec <pkg>@latest        <- marked but unreachable
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process-management semantics only; see issue #2041"
)

# Root of the orphaned tree: a marked MCP launcher that passes the sweep gate.
_ROOT = 500
_ROOT_CMDLINE = b"python3\x00kirocrew_sandbox_x.py"

# The leaked chain hanging off it.
_UNMARKED_INTERMEDIATE = 501
_MARKED_LEAF = 502

# Start-identity tokens. The walk captures one per member; the kill requires it
# to still match, so a recycled PID cannot inherit the verdict.
_TOKEN = {_UNMARKED_INTERMEDIATE: "tok-501", _MARKED_LEAF: "tok-502"}


def _pairs(*pids: int) -> list[tuple[int, str | None]]:
    """(pid, token) pairs in the shape the walk returns."""
    return [(pid, _TOKEN.get(pid, f"tok-{pid}")) for pid in pids]


def _stat_from(child_map: dict[int, list[int]]) -> object:
    """A `_pid_parent_and_token` stub consistent with *child_map*.

    Returns each pid's real parent from the map, so the walk's live-PPid check
    passes for every edge -- the honest baseline. A test that wants a STALE edge
    overrides one pid's answer.
    """
    parent_of = {c: p for p, kids in child_map.items() for c in kids}

    def _stat(pid: int) -> tuple[int | None, str | None]:
        if pid not in parent_of:
            return (None, None)
        return (parent_of[pid], f"tok-{pid}")

    return _stat


@_POSIX_ONLY
class TestOrphanMcpSubtreeReap:
    """kill_orphan_mcps must reap the whole orphaned launcher tree."""

    def _run(
        self,
        descendants: list[tuple[int, str | None]],
        *,
        marked: set[int],
        argv: dict[int, bytes] | None = None,
        tokens: dict[int, str | None] | None = None,
        pgid: int = _ROOT,
        my_pgid: int = 1000,
    ) -> list[int]:
        """Run the sweep over _ROOT and return the PIDs killed in the subtree."""
        from kiro_crew.session_pid import kill_orphan_mcps

        argv_map = argv or {}
        # By default the live token equals the one the walk captured.
        token_map = tokens if tokens is not None else {p: t for p, t in descendants}
        subtree_kills: list[int] = []
        with (
            patch("os.getpgrp", return_value=my_pgid),
            patch("os.getpgid", return_value=pgid),
            patch("os.killpg"),
            patch("os.kill"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", lambda self: _ROOT_CMDLINE),
            patch("kiro_crew.session_pid._build_child_map", return_value={}),
            patch("kiro_crew.session_pid._orphan_descendants", return_value=descendants),
            patch(
                "kiro_crew.session_pid._pid_cmdline",
                side_effect=lambda pid: argv_map.get(pid, b"npm\x00exec\x00pkg@latest"),
            ),
            patch(
                "kiro_crew.session_pid._pid_start_token",
                # The ROOT consults this too now (its own recycle guard), so it
                # must answer stably for _ROOT as well as for each descendant.
                side_effect=lambda pid: token_map.get(pid, f"tok-{pid}"),
            ),
            patch(
                "kiro_crew.session_pid._env_has_kirocrew_marker",
                side_effect=lambda pid: pid in marked,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda pid, _sig: subtree_kills.append(pid),
            ),
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT])
        return subtree_kills

    def test_marked_leaf_behind_unmarked_intermediate_is_reaped(self) -> None:
        """The regression: a marked leaf hidden behind an unmarked parent dies."""
        killed = self._run(
            _pairs(_UNMARKED_INTERMEDIATE, _MARKED_LEAF),
            marked={_ROOT, _UNMARKED_INTERMEDIATE, _MARKED_LEAF},
        )

        assert _MARKED_LEAF in killed, "leaked leaf must not survive the sweep"
        assert _UNMARKED_INTERMEDIATE in killed, "the blocking intermediate must die too"

    def test_kills_leaf_first(self) -> None:
        """Every process dies before its parent, so none reparents mid-kill."""
        killed = self._run(
            _pairs(_UNMARKED_INTERMEDIATE, _MARKED_LEAF),
            marked={_ROOT, _UNMARKED_INTERMEDIATE, _MARKED_LEAF},
        )

        assert killed.index(_MARKED_LEAF) < killed.index(_UNMARKED_INTERMEDIATE)

    def test_descendant_without_env_marker_is_spared(self) -> None:
        """Positive identity per member: no KIROCREW_SPAWNED, no kill."""
        killed = self._run(
            _pairs(_UNMARKED_INTERMEDIATE, _MARKED_LEAF),
            marked={_ROOT, _MARKED_LEAF},  # intermediate is NOT Kiro-Crew-spawned
        )

        assert _UNMARKED_INTERMEDIATE not in killed
        assert _MARKED_LEAF in killed

    def test_peer_gateway_descendant_is_spared(self) -> None:
        """Defence in depth: a gateway argv is refused even if handed in."""
        gateway_pid = 503
        killed = self._run(
            _pairs(gateway_pid, _MARKED_LEAF),
            marked={_ROOT, gateway_pid, _MARKED_LEAF},
            argv={gateway_pid: b"python3\x00-m\x00kiro_crew.cli\x00gateway"},
        )

        assert gateway_pid not in killed, "never signal a peer gateway"
        assert _MARKED_LEAF in killed

    def test_root_is_not_signalled_twice(self) -> None:
        """The root already took killpg/os.kill; the subtree walk skips it."""
        killed = self._run(
            _pairs(_ROOT, _MARKED_LEAF),  # a PID cycle would surface the root here
            marked={_ROOT, _MARKED_LEAF},
        )

        assert _ROOT not in killed

    def test_subtree_counts_against_the_global_kill_cap(self) -> None:
        """Subtree members share _ORPHAN_SWEEP_MAX_KILLS with roots.

        The subtree is reaped BEFORE the root, so it gets the whole remaining
        budget rather than cap - 1 -- and when it consumes all of it the root is
        deliberately left alive (see TestBudgetExhaustionSparesRoot).
        """
        from kiro_crew.session_pid import _ORPHAN_SWEEP_MAX_KILLS

        pids = list(range(600, 600 + _ORPHAN_SWEEP_MAX_KILLS + 10))
        killed = self._run(_pairs(*pids), marked={_ROOT, *pids})

        assert len(killed) == _ORPHAN_SWEEP_MAX_KILLS

    def test_vanished_descendant_is_skipped(self) -> None:
        """A PID that exits between enumeration and kill is not an error."""
        killed = self._run(
            _pairs(_MARKED_LEAF),
            marked={_ROOT, _MARKED_LEAF},
            argv={_MARKED_LEAF: b""},  # unreadable argv == gone
        )

        assert killed == []

    def test_subtree_enumerated_before_root_dies(self) -> None:
        """Enumeration must precede the root signal, or the links are gone."""
        from kiro_crew.session_pid import kill_orphan_mcps

        order: list[str] = []
        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=_ROOT),
            patch("os.killpg", side_effect=lambda *_a: order.append("killpg")),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", lambda self: _ROOT_CMDLINE),
            patch("kiro_crew.session_pid._build_child_map", return_value={}),
            patch(
                "kiro_crew.session_pid._orphan_descendants",
                side_effect=lambda _pid, _map: order.append("enumerate") or [],
            ),
            # The root's own recycle guard reads this twice; a stable answer
            # means "not recycled", which is what this test is about.
            patch("kiro_crew.session_pid._pid_start_token", return_value="tok-root"),
            patch("kiro_crew.session_pid.platform_compat.kill_pid"),
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT])

        assert order == ["enumerate", "killpg"]

    def test_child_map_is_built_once_per_sweep(self) -> None:
        """One /proc pass for the whole sweep, not one per candidate root."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", side_effect=lambda pid: pid),
            patch("os.killpg"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", lambda self: _ROOT_CMDLINE),
            patch("kiro_crew.session_pid._build_child_map", return_value={}) as mock_map,
            patch("kiro_crew.session_pid._orphan_descendants", return_value=[]),
            patch("kiro_crew.session_pid.platform_compat.kill_pid"),
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT, 510, 511])

        assert mock_map.call_count == 1

    def test_child_map_not_built_when_no_mcp_orphan_is_confirmed(self) -> None:
        """A sweep that confirms nothing pays for no /proc pass."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=_ROOT),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            # argv matching no sweep class at all
            patch.object(Path, "read_bytes", lambda self: b"cat\x00/etc/hosts"),
            patch("kiro_crew.session_pid._build_child_map") as mock_map,
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT])

        mock_map.assert_not_called()


@_POSIX_ONLY
class TestPidRecycleGuard:
    """A recycled PID must never inherit a doomed descendant's verdict.

    Regression for a reachable crash: the root's ``killpg`` reaps a descendant,
    the kernel hands that PID to a NEW Kiro-Crew-spawned worker, and the stale
    entry SIGKILLs a live process that passes every other gate.
    """

    def _kill(self, walk_token: str | None, live_token: str | None) -> list[int]:
        from kiro_crew.session_pid import _kill_orphan_mcp_descendants

        killed: list[int] = []
        with (
            patch("os.getpid", return_value=1),
            patch("os.getpgrp", return_value=1000),
            patch("kiro_crew.session_pid._pid_cmdline", return_value=b"npm\x00exec\x00p@latest"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value=live_token),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda pid, _sig: killed.append(pid),
            ),
        ):
            _kill_orphan_mcp_descendants([(_MARKED_LEAF, walk_token)], root=_ROOT, budget=10)
        return killed

    def test_matching_token_is_killed(self) -> None:
        assert self._kill("tok-A", "tok-A") == [_MARKED_LEAF]

    def test_changed_token_means_recycled_and_is_spared(self) -> None:
        """The PID now belongs to someone else -- leave it alone."""
        assert self._kill("tok-A", "tok-B") == []

    def test_missing_live_token_is_unproven_not_a_match(self) -> None:
        assert self._kill("tok-A", None) == []

    def test_missing_walk_token_is_unproven_not_a_match(self) -> None:
        assert self._kill(None, "tok-A") == []

    def test_both_tokens_missing_is_unproven_not_a_match(self) -> None:
        """No identity on either side is not licence to kill.

        The equality check below the guard cannot catch this: ``None != None``
        is False, so without the explicit unproven-identity branch a host whose
        ``get_process_start_id`` never answers would SIGKILL every enumerated
        member with no recycle protection at all.
        """
        assert self._kill(None, None) == []


@_POSIX_ONLY
class TestOrphanMcpSubtreeHelper:
    """Direct cover for _kill_orphan_mcp_descendants edge cases."""

    def test_zero_budget_kills_nothing(self) -> None:
        from kiro_crew.session_pid import _kill_orphan_mcp_descendants

        with patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill:
            killed = _kill_orphan_mcp_descendants(_pairs(_MARKED_LEAF), root=_ROOT, budget=0)

        assert killed == 0
        mock_kill.assert_not_called()

    def test_empty_descendants_kills_nothing(self) -> None:
        from kiro_crew.session_pid import _kill_orphan_mcp_descendants

        with patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill:
            killed = _kill_orphan_mcp_descendants([], root=_ROOT, budget=10)

        assert killed == 0
        mock_kill.assert_not_called()

    def test_never_signals_pid_one_or_self(self) -> None:
        from kiro_crew.session_pid import _kill_orphan_mcp_descendants

        with (
            patch("os.getpid", return_value=4242),
            patch("os.getpgrp", return_value=9999),
            patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill,
        ):
            killed = _kill_orphan_mcp_descendants(_pairs(1, 0, 4242, 9999), root=_ROOT, budget=10)

        assert killed == 0
        mock_kill.assert_not_called()

    def _kill_one(self, **overrides: object) -> int:
        from kiro_crew.session_pid import _kill_orphan_mcp_descendants

        marker = overrides.get("marker", True)
        with (
            patch("os.getpid", return_value=1),
            patch("os.getpgrp", return_value=1000),
            patch("kiro_crew.session_pid._pid_cmdline", return_value=b"npm\x00exec\x00p@latest"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=marker),
            patch("kiro_crew.session_pid._pid_start_token", return_value="tok-502"),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=overrides.get("kill_side_effect"),
            ),
            patch("kiro_crew.session_pid._sel_orphan_mcp_subtree_kill") as mock_sel,
        ):
            killed = _kill_orphan_mcp_descendants(_pairs(_MARKED_LEAF), root=_ROOT, budget=10)
            self._sel = mock_sel
        return killed

    def test_emits_sel_audit_when_it_kills(self) -> None:
        killed = self._kill_one()

        assert killed == 1
        self._sel.assert_called_once_with(_ROOT, 1)

    def test_no_sel_audit_when_nothing_dies(self) -> None:
        killed = self._kill_one(marker=False)

        assert killed == 0
        self._sel.assert_not_called()

    def test_kill_pid_failure_is_not_counted(self) -> None:
        killed = self._kill_one(kill_side_effect=ProcessLookupError)

        assert killed == 0

    def test_signal_passed_is_sigkill(self) -> None:
        from kiro_crew.session_pid import _kill_orphan_mcp_descendants

        with (
            patch("os.getpid", return_value=1),
            patch("os.getpgrp", return_value=1000),
            patch("kiro_crew.session_pid._pid_cmdline", return_value=b"npm\x00exec\x00p@latest"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="tok-502"),
            patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill,
        ):
            _kill_orphan_mcp_descendants(_pairs(_MARKED_LEAF), root=_ROOT, budget=10)

        assert mock_kill.call_args[0][0] == _MARKED_LEAF
        assert mock_kill.call_args[0][1] in (signal.SIGKILL, int(signal.SIGKILL))


@_POSIX_ONLY
class TestOrphanDescendantWalk:
    """The walk traverses the authoritative PPid map, not task/*/children.

    ``_build_child_map`` documents why: ``/proc/<pid>/task/*/children`` needs
    ``CONFIG_PROC_CHILDREN`` and is reliable only for frozen tasks, so on a live
    task it can silently drop whole subtrees -- which is this sweep's own bug
    class, so reaping through it could no-op with no signal.
    """

    def test_walks_depth_first_preorder(self) -> None:
        from kiro_crew.session_pid import _orphan_descendants

        # 11 must have TWO children, or reversing the grandchild push order is a
        # no-op and the ordering is not actually pinned.
        child_map = {10: [11, 20], 11: [12, 13], 20: [21]}
        with (
            patch("kiro_crew.session_pid._prune_from_orphan_walk", return_value=False),
            patch(
                "kiro_crew.session_pid._pid_parent_and_token",
                side_effect=_stat_from(child_map),
            ),
        ):
            walked = _orphan_descendants(10, child_map)

        # Depth-first: 11's whole subtree before 20's, and siblings in map order.
        assert [pid for pid, _ in walked] == [11, 12, 13, 20, 21]

    def test_carries_each_members_start_token(self) -> None:
        """The token is captured HERE so the kill can detect PID recycling."""
        from kiro_crew.session_pid import _orphan_descendants

        with (
            patch("kiro_crew.session_pid._prune_from_orphan_walk", return_value=False),
            patch(
                "kiro_crew.session_pid._pid_parent_and_token",
                side_effect=_stat_from({10: [11]}),
            ),
        ):
            walked = _orphan_descendants(10, {10: [11]})

        assert walked == [(11, "tok-11")]

    def test_cycle_cannot_spin_the_walk(self) -> None:
        from kiro_crew.session_pid import _orphan_descendants

        cyclic = {10: [11], 11: [10]}
        with (
            patch("kiro_crew.session_pid._prune_from_orphan_walk", return_value=False),
            patch(
                "kiro_crew.session_pid._pid_parent_and_token",
                side_effect=_stat_from(cyclic),
            ),
        ):
            walked = _orphan_descendants(10, cyclic)

        assert [pid for pid, _ in walked] == [11]

    def test_childless_root_yields_nothing(self) -> None:
        from kiro_crew.session_pid import _orphan_descendants

        with patch("kiro_crew.session_pid._prune_from_orphan_walk", return_value=False):
            assert _orphan_descendants(10, {}) == []


@_POSIX_ONLY
class TestGatewaySubtreePrune:
    """A peer gateway under an orphan root keeps its WHOLE subtree.

    Regression for a reachable crash: the walk is flat, so excluding a peer
    gateway by its own argv still enumerated its live workers. Those carry
    KIROCREW_SPAWNED and no gateway marker of their own, so each passed the
    per-member gate and would be SIGKILLed -- crashing that pod's sessions.
    """

    _ROOT_PID = 700
    _GATEWAY_PID = 701
    _WORKER_PID = 702
    _SIBLING_PID = 703

    _MAP = {_ROOT_PID: [_GATEWAY_PID, _SIBLING_PID], _GATEWAY_PID: [_WORKER_PID]}
    _ARGV = {
        _GATEWAY_PID: b"python3\x00-m\x00kiro_crew.cli\x00gateway",
        # The worker's OWN argv carries no gateway marker -- this is the trap.
        _WORKER_PID: b"python3\x00-m\x00kiro_crew.mcp_gateway.stub\x00--server\x00x",
        _SIBLING_PID: b"npm\x00exec\x00some-pkg@latest",
    }

    def _walk(self) -> list[int]:
        from kiro_crew.session_pid import _orphan_descendants

        with (
            patch(
                "kiro_crew.session_pid._pid_cmdline",
                side_effect=lambda pid: self._ARGV.get(pid, b"npm\x00exec\x00x@latest"),
            ),
            patch(
                "kiro_crew.session_pid._pid_parent_and_token",
                side_effect=_stat_from(self._MAP),
            ),
        ):
            return [pid for pid, _ in _orphan_descendants(self._ROOT_PID, self._MAP)]

    def test_gateway_worker_is_never_enumerated(self) -> None:
        """The worker must not even reach the kill list."""
        assert self._WORKER_PID not in self._walk(), "a live pod worker would be SIGKILLed"

    def test_gateway_itself_is_not_enumerated(self) -> None:
        assert self._GATEWAY_PID not in self._walk()

    def test_pruning_does_not_hide_a_sibling(self) -> None:
        """The prune is scoped to the gateway's subtree, not the whole walk."""
        assert self._SIBLING_PID in self._walk()

    def test_unreadable_argv_prunes_rather_than_descends(self) -> None:
        """Identity that cannot be established is not a licence to descend."""
        from kiro_crew.session_pid import _prune_from_orphan_walk

        with patch("kiro_crew.session_pid._pid_cmdline", return_value=b""):
            assert _prune_from_orphan_walk(999999) is True

    def test_ordinary_process_is_not_pruned(self) -> None:
        from kiro_crew.session_pid import _prune_from_orphan_walk

        with patch(
            "kiro_crew.session_pid._pid_cmdline",
            return_value=b"npm\x00exec\x00some-pkg@latest",
        ):
            assert _prune_from_orphan_walk(700) is False


@_POSIX_ONLY
class TestPidCmdline:
    """_pid_cmdline is Linux-only by design."""

    def test_off_linux_returns_empty_without_spawning(self) -> None:
        """Off Linux every consumer's verdict is already 'refuse'."""
        from kiro_crew.session_pid import _pid_cmdline

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("subprocess.check_output") as mock_run,
        ):
            mock_sys.platform = "darwin"
            assert _pid_cmdline(700) == b""

        mock_run.assert_not_called()

    def test_unreadable_proc_is_empty(self) -> None:
        from kiro_crew.session_pid import _pid_cmdline

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", side_effect=OSError),
        ):
            mock_sys.platform = "linux"
            assert _pid_cmdline(999999) == b""


@_POSIX_ONLY
class TestStaleMapEdge:
    """A snapshot edge must be re-verified against the LIVE parent.

    Regression for a reachable crash the start token alone could not catch: the
    token pins identity between enumeration and kill, but `child_map` is built
    once per sweep and reused across candidate roots, so an edge can go stale
    BEFORE the walk reads it. The child exits, its PID is reused by a new marked
    worker, and pairing the stale edge with the replacement's fresh token
    produces an entry that matches perfectly at kill time.
    """

    _ROOT_PID = 800
    _RECYCLED = 801
    _HEALTHY = 802
    _MAP = {_ROOT_PID: [_RECYCLED, _HEALTHY]}

    def _walk(self, live_ppid_of_recycled: int | None) -> list[int]:
        from kiro_crew.session_pid import _orphan_descendants

        def _stat(pid: int) -> tuple[int | None, str | None]:
            if pid == self._RECYCLED:
                return (live_ppid_of_recycled, f"tok-{pid}")
            if pid == self._HEALTHY:
                return (self._ROOT_PID, f"tok-{pid}")
            return (None, None)

        with (
            patch("kiro_crew.session_pid._prune_from_orphan_walk", return_value=False),
            patch("kiro_crew.session_pid._pid_parent_and_token", side_effect=_stat),
        ):
            return [pid for pid, _ in _orphan_descendants(self._ROOT_PID, self._MAP)]

    def test_recycled_pid_with_a_different_live_parent_is_dropped(self) -> None:
        """The PID now belongs to someone else's tree -- never signal it."""
        walked = self._walk(live_ppid_of_recycled=999)

        assert self._RECYCLED not in walked, "a recycled PID would be SIGKILLed"

    def test_unreadable_identity_is_dropped(self) -> None:
        """No live parent readable means unproven, so it is not enumerated."""
        assert self._RECYCLED not in self._walk(live_ppid_of_recycled=None)

    def test_a_stale_edge_does_not_hide_its_healthy_sibling(self) -> None:
        assert self._HEALTHY in self._walk(live_ppid_of_recycled=999)

    def test_edge_still_pointing_at_the_parent_is_kept(self) -> None:
        assert self._RECYCLED in self._walk(live_ppid_of_recycled=self._ROOT_PID)

    def test_children_of_a_dropped_edge_are_not_enumerated(self) -> None:
        """Dropping a stale edge drops whatever the snapshot hung beneath it."""
        from kiro_crew.session_pid import _orphan_descendants

        grandchild = 803
        child_map = {self._ROOT_PID: [self._RECYCLED], self._RECYCLED: [grandchild]}

        def _stat(pid: int) -> tuple[int | None, str | None]:
            if pid == self._RECYCLED:
                return (999, "tok-801")  # stale: different live parent
            return (self._RECYCLED, f"tok-{pid}")

        with (
            patch("kiro_crew.session_pid._prune_from_orphan_walk", return_value=False),
            patch("kiro_crew.session_pid._pid_parent_and_token", side_effect=_stat),
        ):
            walked = [pid for pid, _ in _orphan_descendants(self._ROOT_PID, child_map)]

        assert walked == []


@_POSIX_ONLY
class TestWalkIsIterative:
    """A deep chain must not abort the sweep.

    `RecursionError` is not in `kill_orphan_mcps`'s except clause and would fire
    BEFORE the root is signalled, so a recursive walk would abort the whole
    sweep every cycle and preserve the tree it exists to reclaim.
    """

    def test_a_chain_far_past_the_recursion_limit_is_walked(self) -> None:
        from kiro_crew.session_pid import _orphan_descendants

        depth = sys.getrecursionlimit() * 3
        chain = {pid: [pid + 1] for pid in range(9000, 9000 + depth)}

        with (
            patch("kiro_crew.session_pid._prune_from_orphan_walk", return_value=False),
            patch(
                "kiro_crew.session_pid._pid_parent_and_token",
                side_effect=_stat_from(chain),
            ),
        ):
            walked = _orphan_descendants(9000, chain)

        assert len(walked) == depth
        assert [pid for pid, _ in walked] == list(range(9001, 9001 + depth))


@_POSIX_ONLY
class TestPidParentAndToken:
    """Both values must come from ONE stat read."""

    # "pid (comm) state ppid ..." -- comm here contains a space AND parens, the
    # case that breaks a naive whitespace split.
    _STAT = "801 (my (odd) proc) S 800 " + " ".join(str(n) for n in range(5, 23))

    def test_reads_ppid_and_starttime_from_one_stat(self) -> None:
        from kiro_crew.session_pid import _pid_parent_and_token

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_text", lambda self, *a, **k: self._STAT_DATA),
        ):
            mock_sys.platform = "linux"
            Path._STAT_DATA = self._STAT  # type: ignore[attr-defined]
            try:
                ppid, token = _pid_parent_and_token(801)
            finally:
                del Path._STAT_DATA  # type: ignore[attr-defined]

        assert ppid == 800
        # field 22 (starttime) is the 20th token after the last ')'
        assert token == "22"

    def test_off_linux_is_unproven(self) -> None:
        from kiro_crew.session_pid import _pid_parent_and_token

        with patch("kiro_crew.session_pid.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert _pid_parent_and_token(801) == (None, None)

    def test_vanished_pid_is_unproven(self) -> None:
        from kiro_crew.session_pid import _pid_parent_and_token

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_text", side_effect=ProcessLookupError),
        ):
            mock_sys.platform = "linux"
            assert _pid_parent_and_token(999999) == (None, None)

    def test_malformed_stat_is_unproven(self) -> None:
        from kiro_crew.session_pid import _pid_parent_and_token

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_text", lambda self, *a, **k: "no parens here"),
        ):
            mock_sys.platform = "linux"
            assert _pid_parent_and_token(801) == (None, None)


@_POSIX_ONLY
class TestRootRecycleGuard:
    """The ROOT is under the same PID-recycle invariant as its descendants.

    Regression for the third instance of this defect class. The root's cmdline
    re-read used to sit adjacent to its signal, when the branch only called
    `getpgid` in between. Enumerating the subtree put a full `/proc` pass plus a
    `stat` per member in that gap, so the root can exit mid-scan and its PID be
    reused by an ACTIVE MCP process that the `killpg` would then terminate.
    """

    def _sweep(
        self,
        *,
        pre_token: str | None,
        post_token: str | None,
        pgid_before: int = _ROOT,
        pgid_after: int | None = None,
    ) -> dict[str, list[object]]:
        """Run one sweep over _ROOT; report which signals were delivered."""
        from kiro_crew.session_pid import kill_orphan_mcps

        signals: dict[str, list[object]] = {"killpg": [], "kill": [], "descendants": []}
        # Only the ROOT's reads are sequenced (pre-scan, then post-scan); every
        # other pid answers stably, so the descendant path cannot consume the
        # root's values.
        root_token_calls = {"n": 0}
        root_pgid_calls = {"n": 0}

        def _token(pid: int) -> str | None:
            if pid != _ROOT:
                return f"tok-{pid}"
            root_token_calls["n"] += 1
            return pre_token if root_token_calls["n"] == 1 else post_token

        def _getpgid(pid: int) -> int:
            if pid != _ROOT:
                return pgid_before
            root_pgid_calls["n"] += 1
            if root_pgid_calls["n"] == 1 or pgid_after is None:
                return pgid_before
            return pgid_after

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", side_effect=_getpgid),
            patch("os.killpg", side_effect=lambda pg, _s: signals["killpg"].append(pg)),
            patch("os.kill", side_effect=lambda p, _s: signals["kill"].append(p)),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", lambda self: _ROOT_CMDLINE),
            patch("kiro_crew.session_pid._build_child_map", return_value={}),
            patch(
                "kiro_crew.session_pid._orphan_descendants",
                return_value=_pairs(_MARKED_LEAF),
            ),
            patch("kiro_crew.session_pid._pid_start_token", side_effect=_token),
            patch("kiro_crew.session_pid._pid_cmdline", return_value=b"npm\x00exec\x00p@latest"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, _s: signals["descendants"].append(p),
            ),
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT])
        return signals

    def test_stable_root_identity_is_signalled(self) -> None:
        signals = self._sweep(pre_token="tok-root", post_token="tok-root")

        assert signals["killpg"] == [_ROOT]

    def test_root_recycled_during_the_scan_is_not_signalled(self) -> None:
        """The PID now belongs to an active process -- never killpg its group."""
        signals = self._sweep(pre_token="tok-root", post_token="tok-DIFFERENT")

        assert signals["killpg"] == [], "a recycled root's group would be SIGKILLed"
        assert signals["kill"] == []

    def test_root_with_unavailable_identity_is_not_signalled(self) -> None:
        """Unproven identity is not a match -- skip and re-reap next sweep."""
        assert self._sweep(pre_token="tok-root", post_token=None)["killpg"] == []
        assert self._sweep(pre_token=None, post_token="tok-root")["killpg"] == []

    def test_root_with_no_identity_on_either_side_is_not_signalled(self) -> None:
        """The case only the explicit `is None` branch can catch.

        A single missing token already reads as a mismatch through the equality
        check, so it does not isolate that branch. Both missing does: `None !=
        None` is False, so without it a host whose `get_process_start_id` never
        answers would killpg the root's group on no evidence.
        """
        assert self._sweep(pre_token=None, post_token=None)["killpg"] == []

    def test_root_that_left_its_process_group_is_not_signalled(self) -> None:
        """The token proves the process; it cannot prove the group is still its own."""
        signals = self._sweep(
            pre_token="tok-root", post_token="tok-root", pgid_before=_ROOT, pgid_after=4242
        )

        assert signals["killpg"] == []

    def test_identity_is_captured_before_the_subtree_scan(self) -> None:
        """Capturing it after the scan would prove nothing about the gap."""
        from kiro_crew.session_pid import kill_orphan_mcps

        order: list[str] = []
        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=_ROOT),
            patch("os.killpg"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", lambda self: _ROOT_CMDLINE),
            patch(
                "kiro_crew.session_pid._build_child_map",
                side_effect=lambda: order.append("scan") or {},
            ),
            patch("kiro_crew.session_pid._orphan_descendants", return_value=[]),
            patch(
                "kiro_crew.session_pid._pid_start_token",
                side_effect=lambda _p: order.append("token") or "tok-root",
            ),
            patch("kiro_crew.session_pid.platform_compat.kill_pid"),
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT])

        assert order[:3] == ["token", "scan", "token"]


@_POSIX_ONLY
class TestDescendantsBeforeRoot:
    """The root is signalled only after its subtree is accounted for.

    The root is the handle on the tree: it is marked and sweepable, so while it
    lives the whole tree stays re-enumerable next sweep. Killing it first is what
    loses that handle.
    """

    def _order(self) -> list[str]:
        from kiro_crew.session_pid import kill_orphan_mcps

        order: list[str] = []
        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=_ROOT),
            patch("os.killpg", side_effect=lambda *_a: order.append("root")),
            patch("os.kill", side_effect=lambda *_a: order.append("root")),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", lambda self: _ROOT_CMDLINE),
            patch("kiro_crew.session_pid._build_child_map", return_value={}),
            patch(
                "kiro_crew.session_pid._orphan_descendants",
                return_value=_pairs(_MARKED_LEAF),
            ),
            # Must agree with _pairs(): a constant here reads as "recycled" for
            # every descendant and silently skips the whole subtree.
            patch(
                "kiro_crew.session_pid._pid_start_token",
                side_effect=lambda pid: f"tok-{pid}",
            ),
            patch("kiro_crew.session_pid._pid_cmdline", return_value=b"npm\x00exec\x00p@latest"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda *_a: order.append("descendant"),
            ),
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT])
        return order

    def test_descendants_are_signalled_before_the_root(self) -> None:
        order = self._order()

        assert order == ["descendant", "root"]


@_POSIX_ONLY
class TestBudgetExhaustionSparesRoot:
    """When the subtree spends the whole budget the root must SURVIVE.

    Regression for a defect that re-created this PR's own bug: the root died
    first, the descendants got what budget remained, and when the tree exceeded
    the cap the survivors could include the UNMARKED intermediate -- which
    reparents to init, is not sweepable, and hides its marked children behind a
    non-init ppid. Leaving the root alive keeps the remainder discoverable.
    """

    def _run_with(self, descendant_count: int) -> dict[str, list[object]]:
        from kiro_crew.session_pid import kill_orphan_mcps

        signals: dict[str, list[object]] = {"root": [], "descendants": []}
        pids = list(range(600, 600 + descendant_count))
        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=_ROOT),
            patch("os.killpg", side_effect=lambda pg, _s: signals["root"].append(pg)),
            patch("os.kill", side_effect=lambda p, _s: signals["root"].append(p)),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", lambda self: _ROOT_CMDLINE),
            patch("kiro_crew.session_pid._build_child_map", return_value={}),
            patch("kiro_crew.session_pid._orphan_descendants", return_value=_pairs(*pids)),
            # Must agree with _pairs(): a constant here reads as "recycled" for
            # every descendant and silently skips the whole subtree.
            patch(
                "kiro_crew.session_pid._pid_start_token",
                side_effect=lambda pid: f"tok-{pid}",
            ),
            patch("kiro_crew.session_pid._pid_cmdline", return_value=b"npm\x00exec\x00p@latest"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, _s: signals["descendants"].append(p),
            ),
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT])
        return signals

    def test_root_survives_when_the_subtree_spends_the_budget(self) -> None:
        from kiro_crew.session_pid import _ORPHAN_SWEEP_MAX_KILLS

        signals = self._run_with(_ORPHAN_SWEEP_MAX_KILLS + 5)

        assert len(signals["descendants"]) == _ORPHAN_SWEEP_MAX_KILLS
        assert signals["root"] == [], "killing the root would strand the survivors"

    def test_root_is_killed_when_budget_remains(self) -> None:
        signals = self._run_with(2)

        assert len(signals["descendants"]) == 2
        assert signals["root"] == [_ROOT]


@_POSIX_ONLY
class TestRootEvidenceRevalidated:
    """Identity is not the only thing that can go stale before the root signal."""

    def _sweep(
        self,
        *,
        still_eligible: bool = True,
        pgid_after: int | None = None,
    ) -> list[object]:
        from kiro_crew.session_pid import kill_orphan_mcps

        root_signals: list[object] = []
        eligible = iter([True, still_eligible])
        pgid_calls = {"n": 0}

        def _getpgid(_pid: int) -> int:
            pgid_calls["n"] += 1
            if pgid_calls["n"] == 1 or pgid_after is None:
                return _ROOT
            return pgid_after

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", side_effect=_getpgid),
            patch("os.killpg", side_effect=lambda pg, _s: root_signals.append(pg)),
            patch("os.kill", side_effect=lambda p, _s: root_signals.append(p)),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", lambda self: _ROOT_CMDLINE),
            patch("kiro_crew.session_pid._build_child_map", return_value={}),
            patch("kiro_crew.session_pid._orphan_descendants", return_value=[]),
            patch("kiro_crew.session_pid._pid_start_token", return_value="tok-stable"),
            patch(
                "kiro_crew.session_pid._is_sweepable_orphan_mcp",
                side_effect=lambda *_a: next(eligible),
            ),
            patch("kiro_crew.session_pid.platform_compat.kill_pid"),
        ):
            mock_sys.platform = "linux"
            kill_orphan_mcps([_ROOT])
        return root_signals

    def test_root_no_longer_eligible_is_not_signalled(self) -> None:
        """The argv that licensed the kill must still qualify at signal time."""
        assert self._sweep(still_eligible=False) == []

    def test_root_still_eligible_is_signalled(self) -> None:
        assert self._sweep(still_eligible=True) == [_ROOT]

    def test_root_that_changed_group_is_not_signalled(self) -> None:
        assert self._sweep(pgid_after=4242) == []
