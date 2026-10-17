"""Dev Fleet worktree discovery, prune verdicts and removal guards.

Covers ``git worktree list --porcelain`` parsing, the merged / prunable verdicts,
the squash-safe removal race guard, the pod HOME reclamation a removal performs, and
the upstream-remote resolution the sync and the verdicts share.
"""

from __future__ import annotations

import textwrap
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod


# --- worktree porcelain parsing ---
def test_parse_worktree_porcelain_basic():
    from kiro_crew.apps.builtins.dev_fleet.server import _parse_worktree_porcelain

    raw = textwrap.dedent("""\
        worktree /home/user/kirocrew
        HEAD abc1234567890abcdef1234567890abcdef123456
        branch refs/heads/main

        worktree /home/user/kirocrew-wt-feature-x
        HEAD def4567890abcdef1234567890abcdef12345678
        branch refs/heads/feature-x

        worktree /home/user/kirocrew-wt-detached
        HEAD 1234567890abcdef1234567890abcdef12345678
        detached

    """)
    entries = _parse_worktree_porcelain(raw)
    assert len(entries) == 3
    assert entries[0]["path"] == "/home/user/kirocrew"
    assert entries[0]["branch"] == "main"
    assert entries[1]["path"] == "/home/user/kirocrew-wt-feature-x"
    assert entries[1]["branch"] == "feature-x"
    assert entries[2]["branch"] is None  # detached


def test_parse_worktree_porcelain_empty():
    from kiro_crew.apps.builtins.dev_fleet.server import _parse_worktree_porcelain

    assert _parse_worktree_porcelain("") == []


def test_parse_worktree_porcelain_captures_locked():
    """`locked` marks a tree git will refuse to remove.

    It matters that this is parsed rather than discovered from git's stderr:
    `worktree remove` reports the lock LAST, after any pre-removal cleanup has
    already run, so a removal path that only learns about it from the failure
    has already destroyed whatever it cleaned.
    """
    from kiro_crew.apps.builtins.dev_fleet.server import _parse_worktree_porcelain

    raw = textwrap.dedent("""\
        worktree /home/user/kirocrew
        HEAD abc1234567890abcdef1234567890abcdef123456
        branch refs/heads/main

        worktree /home/user/kirocrew-wt-held
        HEAD def4567890abcdef1234567890abcdef12345678
        branch refs/heads/held
        locked keeping this for the repro

        worktree /home/user/kirocrew-wt-bare-lock
        HEAD def4567890abcdef1234567890abcdef12345678
        detached
        locked
    """)
    entries = _parse_worktree_porcelain(raw)
    assert len(entries) == 3
    assert "locked" not in entries[0]
    assert entries[1]["locked"] == "keeping this for the repro"
    # a bare `locked` line still marks the tree, with a placeholder reason
    assert entries[2]["locked"] == "unknown"


def test_parse_worktree_porcelain_captures_prunable():
    """`prunable` marks a record whose checkout directory is gone."""
    from kiro_crew.apps.builtins.dev_fleet.server import _parse_worktree_porcelain

    raw = textwrap.dedent("""\
        worktree /home/user/kirocrew
        HEAD abc1234567890abcdef1234567890abcdef123456
        branch refs/heads/main

        worktree /home/user/kirocrew-wt-deleted
        HEAD def4567890abcdef1234567890abcdef12345678
        branch refs/heads/gone
        prunable gitdir file points to non-existent location

        worktree /home/user/kirocrew-wt-bare-flag
        HEAD def4567890abcdef1234567890abcdef12345678
        detached
        prunable

    """)
    entries = _parse_worktree_porcelain(raw)
    assert len(entries) == 3
    assert "prunable" not in entries[0]
    assert entries[1]["prunable"] == "gitdir file points to non-existent location"
    # A bare `prunable` line (no reason) must still register as prunable.
    assert entries[2]["prunable"] == "unknown"


@pytest.mark.asyncio
async def test_discover_worktrees_drops_prunable():
    """A `rm -rf`'d worktree must not appear in the fleet.

    git keeps reporting the admin record until `git worktree prune` runs, so
    without this filter the ghost row survives every refresh.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    stdout = textwrap.dedent("""\
        worktree /home/user/kirocrew
        HEAD abc1234567890abcdef1234567890abcdef123456
        branch refs/heads/main

        worktree /home/user/kirocrew-wt-alive
        HEAD def4567890abcdef1234567890abcdef12345678
        branch refs/heads/alive

        worktree /home/user/kirocrew-wt-deleted
        HEAD 1234567890abcdef1234567890abcdef12345678
        branch refs/heads/deleted
        prunable gitdir file points to non-existent location

    """)
    with patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(0, stdout, ""))):
        entries = await mod._discover_worktrees()
    paths = [e["path"] for e in entries]
    assert paths == ["/home/user/kirocrew", "/home/user/kirocrew-wt-alive"]
    assert entries[0]["is_main"] is True
    assert entries[1]["is_main"] is False


@pytest.mark.asyncio
async def test_discover_worktrees_keeps_prunable_main():
    """The primary checkout anchors is_main and is never filtered out."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    stdout = textwrap.dedent("""\
        worktree /home/user/kirocrew
        HEAD abc1234567890abcdef1234567890abcdef123456
        branch refs/heads/main
        prunable gitdir file points to non-existent location

        worktree /home/user/kirocrew-wt-alive
        HEAD def4567890abcdef1234567890abcdef12345678
        branch refs/heads/alive

    """)
    with patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(0, stdout, ""))):
        entries = await mod._discover_worktrees()
    assert [e["path"] for e in entries] == [
        "/home/user/kirocrew",
        "/home/user/kirocrew-wt-alive",
    ]
    assert entries[0]["is_main"] is True


# --- PR status ---
def test_pr_status_merged():
    from kiro_crew.apps.builtins.dev_fleet.server import _is_pr_merged

    assert _is_pr_merged({"state": "MERGED", "number": 42}) is True
    assert _is_pr_merged({"state": "OPEN", "number": 42}) is False
    assert _is_pr_merged(None) is False
    assert _is_pr_merged({}) is False


# --- shipped detection (git cherry parsing) ---
@pytest.mark.asyncio
async def test_git_ahead_counts_plus_lines():
    from kiro_crew.apps.builtins.dev_fleet.server import _git_ahead

    cherry_output = "+ abc1234\n+ def5678\n- ghi9012\n"
    with patch(
        "kiro_crew.apps.builtins.dev_fleet.repository._git", new_callable=AsyncMock
    ) as mock_git:
        mock_git.return_value = cherry_output
        result = await _git_ahead("/fake/path")
    assert result == 2


@pytest.mark.asyncio
async def test_git_ahead_returns_none_on_failure():
    from kiro_crew.apps.builtins.dev_fleet.server import _git_ahead

    with patch(
        "kiro_crew.apps.builtins.dev_fleet.repository._git", new_callable=AsyncMock
    ) as mock_git:
        mock_git.return_value = None
        result = await _git_ahead("/fake/path")
    assert result is None


# --- prunable verdict ---
@pytest.mark.asyncio
async def test_prunable_merged_clean():
    """PR merged + clean -> ok:true WITHOUT requiring ahead==0 (squash-safe)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="a" * 40),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40
        ),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is True
    assert v["code"] == "merged"


@pytest.mark.asyncio
async def test_prunable_merged_squash_sim():
    """Squash merge sim: git cherry non-empty (ahead>0) but PR merged -> candidate.

    This is the core bug fix: old code would see ahead>0 and reject with
    'merged_new_commits'. New code does NOT check ahead at all.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=5),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="b" * 40),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="b" * 40
        ),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is True
    assert v["code"] == "merged"


@pytest.mark.asyncio
async def test_prunable_merged_dirty_rejected():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is False
    assert v["code"] == "merged_dirty"


@pytest.mark.asyncio
async def test_prunable_active_unmerged():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "OPEN"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=5),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is False
    assert v["code"] == "active"


# --- removal race guard (squash-safe: OID comparison) ---
@pytest.mark.asyncio
async def test_remove_refuses_when_branch_oid_diverged():
    """Squash-safe race guard: branch OID != PR headRefOid -> refuse removal."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    full_head = "a" * 40
    cache = AsyncMock(return_value={"state": "MERGED"})
    git = AsyncMock(return_value=full_head)
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(fleet_state_mod, "_pr_status_cached", cache),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_git", git),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="b" * 40
        ),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is False
    assert "OID diverged" in result["error"]
    git.assert_any_await("/fake/wt", "rev-parse", "HEAD")
    cache.assert_awaited_once_with("feat-x", full_head)


@pytest.mark.asyncio
async def test_remove_succeeds_when_oid_matches():
    """Squash-safe race guard passes when OIDs match."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is True


# --- session bus graceful degradation ---
@pytest.mark.asyncio
async def test_remove_proceeds_when_session_bus_absent():
    """An absent backend is checked off-loop and leaves no live pod to protect."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew.pod.runtime import PodBackendAbsent

    event_loop_thread = threading.get_ident()
    backend_threads: list[int] = []

    def absent_backend() -> None:
        backend_threads.append(threading.get_ident())
        raise PodBackendAbsent("no session bus")

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=1),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "require_backend", side_effect=absent_backend),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is True
    assert len(backend_threads) == 2
    assert all(thread != event_loop_thread for thread in backend_threads)


@pytest.mark.asyncio
async def test_remove_refuses_stale_explicit_bus_address(monkeypatch):
    """A stale explicit bus address must never authorize worktree removal."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    detail = "Failed to connect to bus: No such file or directory"
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/4242/stale-bus")
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=1),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "IS_MACOS", False),
        patch.object(runtime_mod.rt, "IS_WINDOWS", False),
        patch.object(runtime_mod.rt, "IS_LINUX", True),
        patch.object(runtime_mod.rt.shutil, "which", return_value="/usr/bin/systemctl"),
        patch.object(
            runtime_mod.rt.platform_compat,
            "trusted_system_bin",
            return_value="/usr/bin/systemctl",
        ),
        patch.object(
            runtime_mod.rt,
            "_run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr=detail),
        ),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is False
    assert "cannot verify pod backend" in result["error"]
    assert detail in result["error"]


@pytest.mark.asyncio
async def test_remove_still_fails_on_non_pod_exceptions():
    """Non-PodError exceptions (e.g. OSError, TimeoutError) still refuse removal."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=1),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "require_backend", return_value=None),
        patch.object(runtime_mod.rt, "active_names", side_effect=OSError("disk on fire")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is False
    assert "cannot verify pod state" in result["error"]


# --- stopped-pod HOME reclamation on worktree removal ---
def _remove_stubs(pod_root, **extra):
    """The guard stack every ``_worktree_remove`` pod-path test needs.

    *pod_root* is the test's own ``tmp_path``-derived pod root: the code reads it
    to tell an unreadable root from "nothing to reclaim", so it has to exist and
    be readable, and it must be per-test so nothing is left on disk afterwards.

    Returns the context managers so each test only states the pod-state patches
    it is actually about. A key present in *extra* REPLACES the default of the
    same name rather than stacking a second patch on the same attribute, so a
    test that overrides ``backend`` gets exactly one ``require_backend`` patch.
    """
    base = {
        "find": patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        "dirty": patch.object(
            repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False
        ),
        "pr": patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        "own": patch.object(
            repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=1
        ),
        "git": patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        "head": patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        "cfg": patch.object(
            runtime_mod, "_load_cfg", return_value=SimpleNamespace(pod_root=pod_root)
        ),
        "avail": patch.object(runtime_mod, "_POD_AVAILABLE", True),
        "backend": patch.object(runtime_mod.rt, "require_backend", return_value=None),
        "run": patch.object(
            runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")
        ),
        "upstream": patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    }
    base.update(extra)
    return list(base.values())


@pytest.mark.asyncio
async def test_remove_reclaims_home_of_a_stopped_pod(tmp_path):
    """A pod that is DOWN still owns its isolated HOME, so removal reclaims it.

    Gating reclamation on a live unit stranded the HOME on the common path:
    the operator stops the pod when testing ends and prunes days later once the
    PR merges, so the unit is never active at removal time.
    """
    reclaim = MagicMock(return_value=("reclaimed", ""))
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value=set()),
            orphans=patch.object(runtime_mod.rt, "orphan_homes", return_value=["feat-x"]),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    # The worktree path is handed in as the expected checkout, so attribution
    # inside the lock compares against THIS repo's worktree.
    assert reclaim.call_args.args[1:] == ("feat-x", "/fake/wt")
    # Reported as a reclaim, never as a shutdown that did not happen.
    assert result["reclaimed_pod_home"] is True
    assert result["stopped_pod"] is False


@pytest.mark.asyncio
async def test_remove_leaves_a_non_orphan_home_alone(tmp_path):
    """``orphan_homes`` is the authority: a name it omits is never reclaimed.

    Covers the HOME that does not exist at all and the macOS name mid-``up``
    whose per-pod plist marks it installed rather than orphaned.
    """
    reclaim = MagicMock(return_value=("reclaimed", ""))
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value=set()),
            orphans=patch.object(runtime_mod.rt, "orphan_homes", return_value=["other-wt"]),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    reclaim.assert_not_called()
    assert result["reclaimed_pod_home"] is False


@pytest.mark.asyncio
async def test_remove_refuses_when_home_reclaim_fails(tmp_path):
    """A HOME that survives teardown must not be reported as a clean removal."""
    reclaim = MagicMock(return_value=("failed", "a process is still writing there"))
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value=set()),
            orphans=patch.object(runtime_mod.rt, "orphan_homes", return_value=["feat-x"]),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is False
    assert "pod home reclaim failed" in result["error"]
    assert "still writing" in result["error"]


@pytest.mark.asyncio
async def test_remove_of_active_pod_reports_a_stop_not_a_reclaim(tmp_path):
    """The live-unit path keeps its own reporting; the two flags never conflate."""
    reclaim = MagicMock(return_value=("reclaimed", ""))
    # Stateful rather than a fixed side_effect list: the live-unit path queries
    # liveness three times (pre-stop, post-stop, and the TOCTOU recheck under
    # the git lock), so a length-pinned list breaks on an unrelated change to
    # how often the code verifies.
    calls = {"n": 0}

    def _liveness(_cfg):
        calls["n"] += 1
        return {"feat-x"} if calls["n"] == 1 else set()

    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", side_effect=_liveness),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    assert result["stopped_pod"] is True
    assert result["reclaimed_pod_home"] is False


@pytest.mark.asyncio
async def test_remove_of_an_active_foreign_pod_is_refused(tmp_path):
    """A LIVE pod that is not this checkout's must not be stopped."""
    reclaim = MagicMock(return_value=("foreign", "pinned to a different checkout"))
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value={"feat-x"}),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is False
    assert "refusing pod shutdown" in result["error"]
    assert "different checkout" in result["error"]


@pytest.mark.asyncio
async def test_remove_skips_a_pod_home_that_is_another_checkouts(tmp_path, caplog):
    """A same-basename HOME belonging to ANOTHER checkout is left, not refused.

    Pod identities are global basenames and ``orphan_homes`` keys on the pod
    root, liveness and plist -- never on the checkout pin -- so another repo's
    stale HOME of the same name shows up here. Leaving it is correct; refusing
    this checkout's own removal over it is not.
    """
    reclaim = MagicMock(return_value=("foreign", "pinned to a different checkout"))
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value=set()),
            orphans=patch.object(runtime_mod.rt, "orphan_homes", return_value=["feat-x"]),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        with caplog.at_level("WARNING"):
            result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    assert result["reclaimed_pod_home"] is False
    assert "left a pod HOME named" in caplog.text


@pytest.mark.asyncio
async def test_remove_refuses_when_the_pod_name_was_handed_over(tmp_path):
    """A new pod holding this name blocks the removal, on BOTH reclaim branches.

    Which checkout the new pod belongs to is unknowable here, and it may be
    running out of this very worktree -- so the files must not be deleted, and the
    post-stop liveness recheck cannot be relied on to see a unit that is still
    bootstrapping.
    """
    reclaim = MagicMock(return_value=("handed_over", "was reclaimed by a new pod"))

    # Live-unit branch.
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value={"feat-x"}),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        live = await mod._worktree_remove("feat-x", force=False)

    assert live["ok"] is False
    assert "refusing removal" in live["error"]

    # Orphaned-HOME branch.
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value=set()),
            orphans=patch.object(runtime_mod.rt, "orphan_homes", return_value=["feat-x"]),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        orphan = await mod._worktree_remove("feat-x", force=False)

    assert orphan["ok"] is False
    assert "reclaim failed" in orphan["error"]


@pytest.mark.asyncio
async def test_remove_refuses_when_the_reclaim_itself_raises(tmp_path):
    """A teardown that DIES mid-flight must not be read as a cleanup miss.

    The enumeration's failure degrades, but a raising reclaim (a stop that timed
    out against a still-activating unit) is the state in which removing the
    checkout is unsafe, so it must reach the fail-closed handler.
    """
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value=set()),
            orphans=patch.object(runtime_mod.rt, "orphan_homes", return_value=["feat-x"]),
            reclaim=patch.object(
                worktree_ops_mod,
                "_reclaim_pod_locked",
                MagicMock(side_effect=TimeoutError("stop timed out")),
            ),
        ):
            stack.enter_context(cm)
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is False
    assert "cannot verify pod state" in result["error"]


@pytest.mark.asyncio
async def test_remove_survives_a_failing_home_enumeration(tmp_path, caplog):
    """A cleanup that cannot even enumerate must not refuse the removal.

    Liveness failures fail CLOSED (they guard against deleting a checkout under
    a live pod), but an orphan scan says nothing about liveness -- so it degrades
    to a named leftover instead of turning a lost directory into a lost removal.
    """
    reclaim = MagicMock(return_value=("reclaimed", ""))
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            active=patch.object(runtime_mod.rt, "active_names", return_value=set()),
            orphans=patch.object(
                runtime_mod.rt, "orphan_homes", side_effect=OSError("pod root gone")
            ),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        with caplog.at_level("WARNING"):
            result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    assert result["reclaimed_pod_home"] is False
    reclaim.assert_not_called()
    assert "could not look for" in caplog.text
    assert "pod prune" in caplog.text


@pytest.mark.asyncio
async def test_remove_warns_when_the_pod_root_is_unreadable(tmp_path, caplog):
    """An unreadable pod root must reach the warning, not read as "no orphans".

    ``orphan_homes`` answers ``[]`` on an enumeration error, which is
    indistinguishable from "nothing to reclaim", so the root is read here first.
    """
    reclaim = MagicMock(return_value=("reclaimed", ""))
    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path / "never-created",
            active=patch.object(runtime_mod.rt, "active_names", return_value=set()),
            orphans=patch.object(runtime_mod.rt, "orphan_homes", return_value=[]),
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked", reclaim),
        ):
            stack.enter_context(cm)
        with caplog.at_level("WARNING"):
            result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    reclaim.assert_not_called()
    assert "pod prune" in caplog.text


@pytest.mark.asyncio
async def test_remove_names_the_home_it_cannot_reclaim(tmp_path, caplog):
    """Backend absent: the HOME is unprovable, so it is NAMED, not silently skipped.

    Reclaiming here would risk deleting a live gateway's HOME, so the residue
    stays -- but at a level the operator sees, carrying the path and the verb
    that reclaims it.
    """
    from kiro_crew.pod.runtime import PodBackendAbsent

    with ExitStack() as stack:
        for cm in _remove_stubs(
            tmp_path,
            backend=patch.object(
                runtime_mod.rt, "require_backend", side_effect=PodBackendAbsent("no session bus")
            ),
            unit=patch.object(runtime_mod.rt, "unit_state", return_value=("inactive", 0)),
            home=patch.object(runtime_mod.rt, "pod_home", return_value=Path("/pods/feat-x")),
        ):
            stack.enter_context(cm)
        with caplog.at_level("WARNING"):
            result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    assert "/pods/feat-x" in caplog.text
    assert "pod down feat-x" in caplog.text


# --- _reclaim_pod_locked: attribution and teardown share the per-name lock ---
class _RecordingMutex:
    """Records lock enter/exit against a shared event log."""

    def __init__(self, log):
        self.log = log

    def __call__(self, cfg, name):
        return self

    def __enter__(self):
        self.log.append("lock-enter")
        return self

    def __exit__(self, *exc):
        self.log.append("lock-exit")
        return False


def _reclaim_cfg(tmp_path):
    """A cfg whose ``env_file`` names a path inside the test's own tmp dir."""
    return SimpleNamespace(env_file=lambda name: tmp_path / f"{name}.env")


def test_reclaim_reads_the_pin_and_tears_down_inside_the_lock(tmp_path):
    """The whole point of the helper: no window between attribution and teardown.

    Checking the pin in one process and tearing down in another leaves a gap in
    which a concurrent ``pod up`` from a DIFFERENT checkout claims the same
    global basename, so the teardown would stop that pod and delete its HOME.
    Both halves must land between the same lock enter and exit.
    """
    log = []
    cp = SimpleNamespace(returncode=0, stdout="", stderr="")

    def _pin(_cfg, _name):
        log.append("read-pin")
        return True, "/fake/wt"

    def _stop(_cfg, _name):
        log.append("stop")
        return cp

    with (
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex(log)),
        patch.object(worktree_ops_mod, "_read_pin_strict", _pin),
        patch.object(runtime_mod.rt, "stop_pod", _stop),
    ):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert (outcome, detail) == ("reclaimed", "")
    assert log == ["lock-enter", "read-pin", "stop", "lock-exit"]


def test_reclaim_refuses_a_foreign_pin_without_tearing_down(tmp_path):
    """A pin naming another checkout stops the transaction before any teardown."""
    log = []
    stop = MagicMock()

    with (
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex(log)),
        patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/other/repo/wt")),
        patch.object(runtime_mod.rt, "stop_pod", stop),
    ):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert outcome == "foreign"
    assert "basename collision" in detail
    stop.assert_not_called()
    assert log == ["lock-enter", "lock-exit"]


def test_reclaim_refuses_an_unpinned_pod(tmp_path):
    """No pin at all means the HOME is unattributable -- never torn down.

    Stricter than ``_pod_checkout_guard``, deliberately: the guard allows an
    unpinned name when no unit is live, which is right for operating on a pod the
    caller located, but this path DELETES the HOME and a same-basename leftover
    from another checkout is indistinguishable from here. Liveness is not even
    consulted, so the refusal holds whether or not a unit is up.
    """
    stop = MagicMock()
    active = MagicMock(return_value=set())

    with (
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (False, None)),
        patch.object(runtime_mod.rt, "active_names", active),
        patch.object(runtime_mod.rt, "stop_pod", stop),
    ):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert outcome == "foreign"
    assert "no checkout pin" in detail
    stop.assert_not_called()
    active.assert_not_called()


def test_reclaim_leaves_the_env_file_when_the_name_was_handed_over(tmp_path):
    """A name claimed mid-teardown keeps the NEW pod's pin: never clear it."""
    env = tmp_path / "feat-x.env"
    env.write_text("CHECKOUT=/other/repo/wt\n")
    cfg = SimpleNamespace(env_file=lambda name: env)
    cp = SimpleNamespace(returncode=0, stdout=runtime_mod.rt.RECLAIMED_MARKER, stderr="")

    with (
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")),
        patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp),
    ):
        outcome, detail = mod._reclaim_pod_locked(cfg, "feat-x", "/fake/wt")

    assert outcome == "handed_over"
    assert env.exists()


def test_reclaim_clears_the_env_file_after_a_clean_reclaim(tmp_path):
    """A reclaimed pod's pin is cleared so a later ``up`` re-resolves cleanly."""
    env = tmp_path / "feat-x.env"
    env.write_text("CHECKOUT=/fake/wt\n")
    cfg = SimpleNamespace(env_file=lambda name: env)
    cp = SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")),
        patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp),
    ):
        outcome, _ = mod._reclaim_pod_locked(cfg, "feat-x", "/fake/wt")

    assert outcome == "reclaimed"
    assert not env.exists()


def test_reclaim_reports_a_teardown_that_left_the_home_behind(tmp_path):
    """``stop_pod`` reports a surviving HOME as non-zero; that is a failure.

    The detail reaches the worktree-remove response and so the dashboard, so it
    goes through ``_redact`` like every other detail this helper returns.
    """
    cp = SimpleNamespace(returncode=1, stdout="", stderr="isolated HOME is still at /pods/feat-x")

    with (
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")),
        patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp),
    ):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert outcome == "failed"
    assert "still at /pods/feat-x" in detail


def test_reclaim_redacts_the_teardown_stderr(tmp_path):
    """Teardown stderr can carry a secret from the pod's own output."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    cp = SimpleNamespace(returncode=1, stdout="", stderr=f"stop failed: {secret}")

    with (
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")),
        patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp),
    ):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert outcome == "failed"
    assert secret not in detail
    assert detail == mod._redact(f"stop failed: {secret}")


def test_reclaim_falls_back_to_the_return_code_when_stderr_is_empty(tmp_path):
    """An empty stderr still names a cause rather than an empty error string."""
    cp = SimpleNamespace(returncode=3, stdout="", stderr="   ")

    with (
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")),
        patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp),
    ):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert (outcome, detail) == ("failed", "stop rc=3")


# --- _upstream_remote fallback + override ---
@pytest.mark.asyncio
async def test_upstream_remote_fallback_to_origin():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    repository_mod._UPSTREAM_REMOTE = None
    with patch.object(
        runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(1, "", "not configured")
    ):
        result = await mod._upstream_remote()
    assert result == "origin"
    repository_mod._UPSTREAM_REMOTE = None


@pytest.mark.asyncio
async def test_upstream_remote_reads_config():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    repository_mod._UPSTREAM_REMOTE = None
    with patch.object(
        runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "kirocrew\n", "")
    ):
        result = await mod._upstream_remote()
    assert result == "kirocrew"
    repository_mod._UPSTREAM_REMOTE = None


@pytest.mark.asyncio
async def test_resolve_base_branch_clears_cached_upstream_remote():
    """Re-resolving the base branch replaces the stale cached upstream remote.

    F2 (GPT 5.6): ``_UPSTREAM_REMOTE`` latches on first resolution and is derived
    from the remote the base was resolved against, while ``_rebase_locked`` re-resolves
    the base. A base that moves from a guess to a stated default must NOT keep the
    remote derived from the old resolution, or the worktree is rebased onto a
    ``<stale-remote>/<new-base>`` with no undo. ``_resolve_base_branch`` therefore drops
    the stale value and re-seeds the cache with the remote THIS resolution used, so
    later reads and sync target the resolved remote rather than a stale or wrong one.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    # Save every module global this test mutates: _resolve_base_branch writes
    # BASE_BRANCH and _BASE_BRANCH_POSITIVE, which other tests read at their default.
    _saved_base = repository_mod.BASE_BRANCH
    _saved_positive = repository_mod._BASE_BRANCH_POSITIVE
    # A stale remote cached against a prior base.
    repository_mod._UPSTREAM_REMOTE = "fork"

    # Drive tier-1 resolution: origin advertises HEAD -> trunk (live ls-remote).
    async def fake_git(_repo, *args, **kwargs):
        if args[:1] == ("remote",):
            return "origin\nfork"
        if args[:1] == ("ls-remote",) and args[-1] == "HEAD":
            return "ref: refs/heads/trunk\tHEAD\n<sha>\tHEAD\n"
        return ""

    try:
        with (
            patch.object(repository_mod, "_repo", return_value="/fake/repo"),
            patch.object(repository_mod, "_git", new=AsyncMock(side_effect=fake_git)),
        ):
            await mod._resolve_base_branch()

        # The stale 'fork' is gone; the cache now holds the remote this resolution used.
        assert repository_mod._UPSTREAM_REMOTE == "origin"
        assert repository_mod.BASE_BRANCH == "trunk"
        assert repository_mod._BASE_BRANCH_POSITIVE is True
    finally:
        repository_mod.BASE_BRANCH = _saved_base
        repository_mod._BASE_BRANCH_POSITIVE = _saved_positive
        repository_mod._UPSTREAM_REMOTE = None
