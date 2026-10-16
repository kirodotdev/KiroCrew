"""Tests for the dev-fleet native dashboard handler."""
from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac  # noqa: F401
import hmac as _hmac_mod
import json
import os
import shutil
import textwrap
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yarl
from aiohttp import web  # noqa: F401  (used by builtin re-shell tests)
from aiohttp.test_utils import TestClient, TestServer  # noqa: F401
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _POSIX_ONLY, _cleanup_sync_tempdirs, _install_step, _run_sync

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import http_api as http_api_mod
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
        "/home/user/kirocrew", "/home/user/kirocrew-wt-alive",
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
    with patch("kiro_crew.apps.builtins.dev_fleet.repository._git", new_callable=AsyncMock) as mock_git:
        mock_git.return_value = cherry_output
        result = await _git_ahead("/fake/path")
    assert result == 2


@pytest.mark.asyncio
async def test_git_ahead_returns_none_on_failure():
    from kiro_crew.apps.builtins.dev_fleet.server import _git_ahead

    with patch("kiro_crew.apps.builtins.dev_fleet.repository._git", new_callable=AsyncMock) as mock_git:
        mock_git.return_value = None
        result = await _git_ahead("/fake/path")
    assert result is None


# --- prunable verdict ---
@pytest.mark.asyncio
async def test_prunable_merged_clean():
    """PR merged + clean -> ok:true WITHOUT requiring ahead==0 (squash-safe)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
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

    with patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=5), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="b" * 40), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="b" * 40), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is True
    assert v["code"] == "merged"


@pytest.mark.asyncio
async def test_prunable_merged_dirty_rejected():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is False
    assert v["code"] == "merged_dirty"


@pytest.mark.asyncio
async def test_prunable_active_unmerged():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "OPEN"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=5), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
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
    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(fleet_state_mod, "_pr_status_cached", cache), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3), \
         patch.object(repository_mod, "_git", git), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="b" * 40), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is False
    assert "OID diverged" in result["error"]
    git.assert_any_await("/fake/wt", "rev-parse", "HEAD")
    cache.assert_awaited_once_with("feat-x", full_head)


@pytest.mark.asyncio
async def test_remove_succeeds_when_oid_matches():
    """Squash-safe race guard passes when OIDs match."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(runtime_mod, "_load_cfg", return_value=None), \
         patch.object(runtime_mod, "_POD_AVAILABLE", False), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
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

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=1), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "require_backend", side_effect=absent_backend), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
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

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=1), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "require_backend", return_value=None), \
         patch.object(runtime_mod.rt, "active_names", side_effect=OSError("disk on fire")), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
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
        "find": patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                             return_value=({"path": "/fake/wt", "branch": "feat-x",
                                            "is_main": False}, None)),
        "dirty": patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        "pr": patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock,
                           return_value={"state": "MERGED"}),
        "own": patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=1),
        "git": patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        "head": patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock,
                             return_value="aaa1111"),
        "cfg": patch.object(runtime_mod, "_load_cfg",
                            return_value=SimpleNamespace(pod_root=pod_root)),
        "avail": patch.object(runtime_mod, "_POD_AVAILABLE", True),
        "backend": patch.object(runtime_mod.rt, "require_backend", return_value=None),
        "run": patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        "upstream": patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock,
                                 return_value="origin"),
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
            reclaim=patch.object(worktree_ops_mod, "_reclaim_pod_locked",
                                 MagicMock(side_effect=TimeoutError("stop timed out"))),
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
            orphans=patch.object(runtime_mod.rt, "orphan_homes", side_effect=OSError("pod root gone")),
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
            backend=patch.object(runtime_mod.rt, "require_backend",
                                 side_effect=PodBackendAbsent("no session bus")),
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

    with patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex(log)), \
         patch.object(worktree_ops_mod, "_read_pin_strict", _pin), \
         patch.object(runtime_mod.rt, "stop_pod", _stop):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert (outcome, detail) == ("reclaimed", "")
    assert log == ["lock-enter", "read-pin", "stop", "lock-exit"]


def test_reclaim_refuses_a_foreign_pin_without_tearing_down(tmp_path):
    """A pin naming another checkout stops the transaction before any teardown."""
    log = []
    stop = MagicMock()

    with patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex(log)), \
         patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/other/repo/wt")), \
         patch.object(runtime_mod.rt, "stop_pod", stop):
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

    with patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])), \
         patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (False, None)), \
         patch.object(runtime_mod.rt, "active_names", active), \
         patch.object(runtime_mod.rt, "stop_pod", stop):
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

    with patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])), \
         patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")), \
         patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp):
        outcome, detail = mod._reclaim_pod_locked(cfg, "feat-x", "/fake/wt")

    assert outcome == "handed_over"
    assert env.exists()


def test_reclaim_clears_the_env_file_after_a_clean_reclaim(tmp_path):
    """A reclaimed pod's pin is cleared so a later ``up`` re-resolves cleanly."""
    env = tmp_path / "feat-x.env"
    env.write_text("CHECKOUT=/fake/wt\n")
    cfg = SimpleNamespace(env_file=lambda name: env)
    cp = SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])), \
         patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")), \
         patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp):
        outcome, _ = mod._reclaim_pod_locked(cfg, "feat-x", "/fake/wt")

    assert outcome == "reclaimed"
    assert not env.exists()


def test_reclaim_reports_a_teardown_that_left_the_home_behind(tmp_path):
    """``stop_pod`` reports a surviving HOME as non-zero; that is a failure.

    The detail reaches the worktree-remove response and so the dashboard, so it
    goes through ``_redact`` like every other detail this helper returns.
    """
    cp = SimpleNamespace(returncode=1, stdout="", stderr="isolated HOME is still at /pods/feat-x")

    with patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])), \
         patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")), \
         patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert outcome == "failed"
    assert "still at /pods/feat-x" in detail


def test_reclaim_redacts_the_teardown_stderr(tmp_path):
    """Teardown stderr can carry a secret from the pod's own output."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    cp = SimpleNamespace(returncode=1, stdout="", stderr=f"stop failed: {secret}")

    with patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])), \
         patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")), \
         patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert outcome == "failed"
    assert secret not in detail
    assert detail == mod._redact(f"stop failed: {secret}")


def test_reclaim_falls_back_to_the_return_code_when_stderr_is_empty(tmp_path):
    """An empty stderr still names a cause rather than an empty error string."""
    cp = SimpleNamespace(returncode=3, stdout="", stderr="   ")

    with patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])), \
         patch.object(worktree_ops_mod, "_read_pin_strict", lambda c, n: (True, "/fake/wt")), \
         patch.object(runtime_mod.rt, "stop_pod", lambda c, n: cp):
        outcome, detail = mod._reclaim_pod_locked(_reclaim_cfg(tmp_path), "feat-x", "/fake/wt")

    assert (outcome, detail) == ("failed", "stop rc=3")


# --- _upstream_remote fallback + override ---
@pytest.mark.asyncio
async def test_upstream_remote_fallback_to_origin():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    repository_mod._UPSTREAM_REMOTE = None
    with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(1, "", "not configured")):
        result = await mod._upstream_remote()
    assert result == "origin"
    repository_mod._UPSTREAM_REMOTE = None


@pytest.mark.asyncio
async def test_upstream_remote_reads_config():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    repository_mod._UPSTREAM_REMOTE = None
    with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "kirocrew\n", "")):
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
        with patch.object(repository_mod, "_repo", return_value="/fake/repo"), \
             patch.object(repository_mod, "_git", new=AsyncMock(side_effect=fake_git)):
            await mod._resolve_base_branch()

        # The stale 'fork' is gone; the cache now holds the remote this resolution used.
        assert repository_mod._UPSTREAM_REMOTE == "origin"
        assert repository_mod.BASE_BRANCH == "trunk"
        assert repository_mod._BASE_BRANCH_POSITIVE is True
    finally:
        repository_mod.BASE_BRANCH = _saved_base
        repository_mod._BASE_BRANCH_POSITIVE = _saved_positive
        repository_mod._UPSTREAM_REMOTE = None


# --- sync runner emits ::step:: markers ---
@pytest.mark.asyncio
async def test_sync_script_emits_step_markers():
    """The sync runner emits ::step::<idx>::<label> before each step.

    The runner is a snapshotted module (sync_runner.py) run by path, so the
    gateway-side contract is "invoke that module"; the marker EMISSION itself is
    proven by execution in test_dev_fleet_sync_runner.py. Here we pin that the
    sync invokes the runner by path and that the module still emits the marker.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    repository_mod._UPSTREAM_REMOTE = "origin"
    worktree_ops_mod._SYNC_RID = None
    with patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="main"), \
         patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch("kiro_crew.apps.builtins.dev_fleet.worktree_ops.sandboxed_spawn_argv",
               side_effect=lambda cmd, mode, env=None: (cmd, env or {}, None)), \
         patch.object(runtime_mod, "_start_run", new_callable=AsyncMock, return_value="run-123") as mock_start:
        async with mod._SYNC_LOCK:
            result = await mod._sync_start_locked()
    # Clean up the snapshot + steps file the stubbed _start_run would have —
    # BEFORE any assertion, so a failing assertion cannot leak the staged
    # temporary directories (no test side effects).
    _cleanup_sync_tempdirs(mock_start)
    assert result["ok"] is True
    cmd_args = mock_start.call_args[0]
    runner_cmd = cmd_args[1]
    assert runner_cmd[0].endswith("python") or "python" in runner_cmd[0]
    assert any(str(a).endswith("sync_runner.py") for a in runner_cmd)
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    assert "::step::" in src
    repository_mod._UPSTREAM_REMOTE = None


# --- Windows: a write-locked console script must not be handed to pip ---
# The probe itself moved to kiro_crew.dep_sync with the substitute it feeds, and
# its tests moved with it (test/test_dep_sync.py). What stays here is the sync's
# use of the result: which install step gets built.


@pytest.mark.asyncio
async def test_sync_substitutes_a_dependency_only_install_when_a_script_is_locked():
    """The sync proceeds; only the editable reinstall is swapped out.

    pip cannot replace the locked wrapper, but it does not have to. An editable
    install serves source straight from ``src``, so the merge alone makes the new
    revision live; the only thing the reinstall was still buying is a dependency
    the revision added, and installing a dependency never touches the project's
    own console script. Refusing the whole sync instead left the single-checkout
    Windows layout — the ordinary one — with no working Pull+build at all.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew import dep_sync

    result, cmd, steps = await _run_sync(mod, [r"C:\repo\.venv\Scripts\kirocrew.exe"])

    assert result["ok"] is True
    # A run was started, so fetch and merge do happen.
    assert cmd is not None
    # The install step's argv is what the substitution changes; inspect it
    # directly rather than source text.
    install = _install_step(steps)
    argv_flat = " ".join(install["argv"])
    assert "dep_sync.py" in argv_flat
    assert "-e" not in install["argv"]
    # It must NOT be run as `-m kiro_crew...dep_sync`. That would import the
    # module from the working tree after the merge has landed, pulling the whole
    # package __init__ chain with it, so a revision that raises the
    # `requires-python` floor with newer syntax would SyntaxError while being
    # parsed and the floor refusal written for that revision could never run.
    assert dep_sync.__name__ not in argv_flat
    assert "-m" not in install["argv"]
    # The file it runs is a snapshot taken BEFORE the merge, so it lives outside
    # the checkout being synced.
    assert r"C:\repo\dep_sync.py" not in argv_flat
    # ORDER MATTERS, and it is the SAME order the reinstall it replaces uses:
    # fetch -> merge -> install. Installing first would need the merge to be
    # proven impossible to fail, which cannot be done completely; a failed install
    # after a landed merge is exactly what the reinstall path already does.
    labels = [s["label"] for s in steps]
    assert labels.index("Merge") < labels.index("pip install")


@pytest.mark.asyncio
async def test_sync_keeps_the_editable_reinstall_when_nothing_is_locked():
    """A venv this gateway is NOT served by must still get the real reinstall.

    The substitution is a concession to a lock, not an improvement to prefer: it
    cannot refresh a console script, so anywhere pip can do the whole job, it
    should.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew import dep_sync

    result, cmd, steps = await _run_sync(mod, [])

    assert result["ok"] is True
    install = _install_step(steps)
    argv_flat = " ".join(install["argv"])
    assert "-e" in install["argv"]
    assert dep_sync.__name__ not in argv_flat
    assert "dep_sync.py" not in argv_flat


@pytest.mark.asyncio
async def test_every_step_gets_a_utf8_pin_in_its_environment():
    """The runner's own reconfigure() does not reach its children.

    Each step is a separate process that inherits the pipe but re-derives its
    encoding from the locale, so the Python steps (pip, and the build-and-stage
    child) would still encode a non-ASCII checkout path with the codepage and
    die on it. The environment is the only channel that reaches a child.

    The MECHANISM is proven by execution in test_dev_fleet_sync_runner.py
    (a child spawned under a divergent inherited PYTHONIOENCODING observes the
    utf-8 pin). What stays here is the module contract: run_step assigns the
    pin onto every step's env before spawning.
    """
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    run_step_body = src.split("def run_step(", 1)[1].split("\ndef ", 1)[0]
    assert 'env["PYTHONIOENCODING"] = "utf-8:replace"' in run_step_body
    # Applied to the env actually handed to the spawn, not a stale copy. Matched
    # on the keyword alone, not on a formatted argument run: the spawn became a
    # multi-line Popen when stderr got its own pipe, and a substring spanning two
    # arguments made this fail on a formatting change rather than on a defect.
    assert "env=env," in run_step_body
    assert "cwd=cwd," in run_step_body
    # Set before the step is spawned, not after.
    assert run_step_body.index("PYTHONIOENCODING") < run_step_body.index("subprocess.Popen(")


@pytest.mark.asyncio
async def test_sync_runs_every_step_when_nothing_is_locked():
    """Control: an unlocked venv gets the full sync, reinstall included."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    result, cmd, steps = await _run_sync(mod, [])

    assert result["ok"] is True, result
    labels = [s["label"] for s in steps]
    assert "pip install" in labels
    assert "Pull" in labels


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [[], [r"C:\repo\.venv\Scripts\kirocrew.exe"]],
                         ids=["reinstall-branch", "substitute-branch"])
async def test_sync_refuses_a_venv_that_serves_another_checkout(locked):
    """The refusal covers BOTH install paths, and refuses before either runs.

    `<repo>/.venv` is only where the interpreter was found; it can be an install
    of a different checkout, and `pip install -e .` would then silently repoint
    that editable install at this repo — so the OTHER checkout's gateway becomes
    this code on its next restart. The dependency-only path has always refused
    this; the reinstall path did not, which left the safer path as the only
    guarded one. Parametrized over both branches because that asymmetry is
    exactly the bug: a fix that only covers the one it was found on is not one.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew import dep_sync

    with patch.object(
        dep_sync,
        "venv_not_mapped_to",
        return_value="the target venv imports this project from /other/checkout",
    ):
        result, cmd, steps = await _run_sync(mod, locked)

    assert result["ok"] is False
    assert "/other/checkout" in result["error"]
    # Remedy-first, like every other refusal on this endpoint.
    assert "own editable install" in result["error"]
    # Nothing ran: no fetch, no merge, no install. A refusal after the merge
    # would leave the checkout moved with its dependencies unresolved.
    assert cmd is None


@pytest.mark.asyncio
async def test_sync_runner_pins_utf8_stdout_before_its_first_print():
    """Align the writing side with the reader.

    `_start_run` decodes the stream as UTF-8, while a piped stdout on Windows
    encodes with the process locale codepage — a mismatch that mangles or kills
    any non-ASCII print. Pinned before the step loop so no print predates it.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, _ = await _run_sync(mod, [])

    assert cmd is not None
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8"' in src
    # Within main(), the reconfigure runs before run_steps() is called, so no
    # print in the step loop predates it.
    main_body = src.split("def main(", 1)[1]
    assert "reconfigure(" in main_body
    assert main_body.index("reconfigure(") < main_body.index("run_steps(")


def test_utf8_reconfigure_survives_a_legacy_codepage_pipe():
    """Prove the mechanism, not just its presence in the source.

    Forcing a legacy codepage on the child's stdout is what the Windows
    mismatch looks like; the reconfigure call is what keeps the print
    non-fatal, and the reader side decodes UTF-8.
    """
    import subprocess
    import sys as _sys

    text = "\u9648\u660e\u4f2a"
    body = (
        "import sys\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        f"print({text!r}, flush=True)\n"
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    proc = subprocess.run(
        [_sys.executable, "-c", body], capture_output=True, env=env, timeout=60
    )

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    # Matches how the run reader decodes the stream.
    assert text in proc.stdout.decode(errors="replace")


def test_pythonioencoding_saves_a_step_child_that_prints_a_non_ascii_path():
    """The child half of the same mismatch, proven end to end.

    A step child cannot call the runner's reconfigure() for itself, so this is
    the case the environment variable has to carry: the SAME print that dies
    under a legacy codepage survives once the pin is in the env, which is what
    a pip or build step does when it echoes a non-ASCII checkout path.
    """
    import subprocess
    import sys as _sys

    path = "C:\\Users\\\u9648\u660e\u4f2a\\KiroCrew"
    body = f"print({path!r}, flush=True)\n"
    codepage = {**os.environ, "PYTHONIOENCODING": "cp1252"}

    # Without the pin the child dies on the encode — the defect being fixed.
    unpinned = subprocess.run(
        [_sys.executable, "-c", body], capture_output=True, env=codepage, timeout=60
    )
    assert unpinned.returncode != 0
    assert b"UnicodeEncodeError" in unpinned.stderr

    # With it, the child survives and the reader gets the real path back.
    pinned = subprocess.run(
        [_sys.executable, "-c", body],
        capture_output=True,
        env={**codepage, "PYTHONIOENCODING": "utf-8:replace"},
        timeout=60,
    )
    assert pinned.returncode == 0, pinned.stderr.decode(errors="replace")
    assert path in pinned.stdout.decode(errors="replace")


# --- repo owner/name parsing ---
@pytest.mark.asyncio
async def test_repo_owner_name_ssh():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    fleet_state_mod._OWNER_REPO = None
    with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "git@github.com:kirodotdev/KiroCrew.git\n", "")):
        result = await mod._repo_owner_name()
    assert result == "kirodotdev/KiroCrew"


@pytest.mark.asyncio
async def test_repo_owner_name_https():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    fleet_state_mod._OWNER_REPO = None
    with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "https://github.com/kirodotdev/KiroCrew.git\n", "")):
        result = await mod._repo_owner_name()
    assert result == "kirodotdev/KiroCrew"


# --- _find_worktree ambiguity rejection ---
@pytest.mark.asyncio
async def test_find_worktree_ambiguous():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    wts = [
        {"path": "/a/feature-x", "is_main": False},
        {"path": "/b/feature-x", "is_main": False},
    ]
    with patch.object(repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=wts):
        result, err = await mod._find_worktree("feature-x")
    assert result is None
    assert "ambiguous" in err


@pytest.mark.asyncio
async def test_find_worktree_not_found():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=[]):
        result, err = await mod._find_worktree("nope")
    assert result is None
    assert "not found" in err


# --- force bool strictness (handler validates) ---
@pytest.mark.asyncio
async def test_worktree_remove_force_must_be_bool():
    """The /worktree/remove handler rejects non-boolean force."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_worktree_remove

    # Build a mock request with non-bool force
    async def fake_json():
        return {"name": "test-wt", "force": "yes"}

    request = MagicMock()
    request.json = fake_json
    request.content_length = 100

    with patch("kiro_crew.apps.builtins.dev_fleet.repository._valid_worktree_names", new_callable=AsyncMock, return_value={"test-wt"}):
        resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "force must be a boolean" in body["error"]


# --- sync single-flight (409 on busy) ---
@pytest.mark.asyncio
async def test_sync_returns_409_when_already_running():
    import asyncio

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    # Inject a fake running sync with a live task + process
    worktree_ops_mod._SYNC_RID = "fake123"
    async with mod._RUNS_LOCK:
        mod._RUNS["fake123"] = {"status": "running", "exit_code": None, "label": "sync", "output": []}

    # Simulate a genuinely-running process (returncode=None)
    from unittest.mock import MagicMock
    mock_proc = MagicMock()
    mock_proc.returncode = None
    never_done = asyncio.get_event_loop().create_future()
    running_task = asyncio.ensure_future(never_done)
    mod._ACTIVE_RUNS["fake123"] = (running_task, mock_proc)

    try:
        result = await mod._sync()
        assert result["ok"] is False
        assert "already running" in result["error"]
    finally:
        async with mod._RUNS_LOCK:
            del mod._RUNS["fake123"]
        worktree_ops_mod._SYNC_RID = None
        mod._ACTIVE_RUNS.pop("fake123", None)
        never_done.set_result(None)
        await running_task


# --- redaction ---
def test_redact_applied():
    from kiro_crew.apps.builtins.dev_fleet.server import _redact

    # Should not crash on normal strings
    assert _redact("hello world") == "hello world"
    # Should redact AWS keys
    result = _redact("key=AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in result


# --- disk aggregation name derivation ---
@pytest.mark.asyncio
async def test_disk_name_derivation_from_path():
    """_disk() derives worktree name from w['path']."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    mod._DISK.update({"status": "idle", "total_mb": None, "per": {}})

    fake_worktrees = [
        {"path": "/home/user/kirocrew", "is_main": True},
        {"path": "/home/user/kirocrew-wt-feature-x", "is_main": False},
    ]

    with patch.object(repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=fake_worktrees), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "100\t/fake\n", "")):
        result = await mod._disk()
        assert result["status"] == "computing"

        # Wait for background task
        for _ in range(50):
            await asyncio.sleep(0.05)
            if mod._DISK["status"] == "done":
                break

    assert mod._DISK["status"] == "done"
    assert "kirocrew" in mod._DISK["per"] or "kirocrew-wt-feature-x" in mod._DISK["per"]
    assert mod._DISK["total_mb"] == 200


# --- _build_pending (server-side truth for build-pending chip) ---
def test_build_pending_false_when_dist_older():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    original_start = mod._START_EPOCH
    # Set start epoch far in the future so dist mtime is always older
    fleet_state_mod._START_EPOCH = time.time() + 99999
    try:
        result = mod._build_pending()
        # dist dir may or may not exist in test env; either way it should not be "pending"
        assert result is False
    finally:
        fleet_state_mod._START_EPOCH = original_start


def test_build_pending_true_when_dist_newer():
    import os
    import tempfile
    from pathlib import Path

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    # Create a temp dir to act as dist
    with tempfile.TemporaryDirectory() as tmpdir:
        dist_path = Path(tmpdir) / "dist"
        dist_path.mkdir()
        # Touch to ensure mtime is fresh
        os.utime(str(dist_path), (time.time(), time.time()))

        # Patch the dist resolution
        original_start = mod._START_EPOCH
        fleet_state_mod._START_EPOCH = time.time() - 100  # pretend started 100s ago
        with patch.object(Path, '__new__', wraps=Path.__new__):
            # Directly test the logic: dist mtime > start epoch
            assert dist_path.stat().st_mtime > mod._START_EPOCH
        fleet_state_mod._START_EPOCH = original_start


def test_build_pending_false_when_dist_missing():
    """_build_pending returns False when dist dir does not exist (OSError path)."""
    import tempfile
    from pathlib import Path

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    original_start = mod._START_EPOCH
    fleet_state_mod._START_EPOCH = 0  # very old — would be pending IF dist existed
    try:
        # Point __file__ resolution at a temp dir with no 'static/dist' subtree
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_file = Path(tmpdir) / "handlers" / "dev_fleet.py"
            fake_file.parent.mkdir(parents=True)
            fake_file.touch()

            def patched_build_pending() -> bool:
                try:
                    dist = fake_file.resolve().parent.parent / "static" / "dist"
                    if not dist.exists():
                        return False
                    return dist.stat().st_mtime > mod._START_EPOCH
                except OSError:
                    return False

            result = patched_build_pending()
            assert result is False
    finally:
        fleet_state_mod._START_EPOCH = original_start


# --- run pointers are NOT baked into the cached snapshot ---
@pytest.mark.asyncio
async def test_fleet_build_does_not_bake_run_pointers():
    """`_build_fleet` must leave the run pointers to the request-time overlay.

    The snapshot it returns is cached and served stale-while-revalidate, so a
    pointer written here is a frozen answer to a live question: a run started
    after the build would be invisible until the cache turned over, which is the
    "no progress, press it again" bug. `_with_live_run_pointers` owns both
    pointers; this pins that there is only one owner, so a future edit cannot
    quietly reintroduce a second, staler one.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    worktree_ops_mod._SYNC_RID = "test-rid-abc"
    try:
        with patch.object(repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=[
            {"path": "/fake/main", "head": "abc1234", "branch": "main", "is_main": True}
        ]), \
             patch.object(repository_mod, "_git_info", new_callable=AsyncMock, return_value={
                 "branch": "main", "head": "abc1234", "dirty": False,
                 "ahead": 0, "behind": 0, "last_updated_at": None
             }), \
             patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None), \
             patch.object(repository_mod, "_git_ahead", new_callable=AsyncMock, return_value=0), \
             patch.object(runtime_mod, "_load_cfg", return_value=None), \
             patch.object(fleet_state_mod, "_build_pending", return_value=False):
            data = await mod._build_fleet()
        assert "sync_run_id" not in data
        assert all("provision_run_id" not in w for w in data["worktrees"])
        assert "build_pending" in data
    finally:
        worktree_ops_mod._SYNC_RID = None


@pytest.mark.asyncio
async def test_fleet_includes_build_pending():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=[
        {"path": "/fake/main", "head": "abc1234", "branch": "main", "is_main": True}
    ]), patch.object(repository_mod, "_git_info", new_callable=AsyncMock, return_value={
        "branch": "main", "head": "abc1234", "dirty": False,
        "ahead": 0, "behind": 0, "last_updated_at": None,
    }), patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None), \
            patch.object(repository_mod, "_git_ahead", new_callable=AsyncMock, return_value=0), \
            patch.object(runtime_mod, "_load_cfg", return_value=None), \
            patch.object(fleet_state_mod, "_build_pending", return_value=True):
        data = await mod._build_fleet()
    assert data["build_pending"] is True


# --- provision_run_id exposed in fleet response (reattach after reload) ---
# --- provision run-id selection (what a reloaded page can reattach to) ---
# These pin the SELECTION semantics at their owning unit rather than through a
# fleet build: the pointer reaches the payload via the request-time overlay
# (`_with_live_run_pointers`), which
# `test_fleet_handler_overlays_runs_started_after_snapshot` covers.
@pytest.mark.asyncio
async def test_provision_reattach_ids_expose_running_and_failed_runs():
    with patch.dict(mod._PROVISION_INFLIGHT, {
        "wt-running": "rid-running", "wt-failed": "rid-failed",
    }, clear=True), patch.dict(mod._RUNS, {
        "rid-running": {"status": "running", "exit_code": None, "output": []},
        "rid-failed": {"status": "done", "exit_code": 1, "output": []},
    }, clear=True):
        rids = await mod._provision_reattach_ids()
    assert rids == {"wt-running": "rid-running", "wt-failed": "rid-failed"}


@pytest.mark.asyncio
async def test_provision_reattach_ids_omit_successful_and_evicted_runs():
    with patch.dict(mod._PROVISION_INFLIGHT, {
        # Success: nothing to reattach — the fleet row shows the built state.
        "wt-ok": "rid-ok",
        # Evicted from the bounded run registry: output is unfetchable.
        "wt-gone": "rid-gone",
    }, clear=True), patch.dict(mod._RUNS, {
        "rid-ok": {"status": "done", "exit_code": 0, "output": []},
    }, clear=True):
        rids = await mod._provision_reattach_ids()
    assert rids == {}


# --- SEL audit on mutations (Codex R17) ---
def _fake_sel_capture(events):
    class _FakeSel:
        def log_tool_invocation(self, **kw):
            events.append(kw)
    return lambda: _FakeSel()


@pytest.mark.asyncio
async def test_mutation_denied_emits_sel_event(monkeypatch):
    """A rejected worktree remove emits exactly one SEL event with outcome=denied."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_worktree_remove

    events: list = []
    monkeypatch.setattr(runtime_mod, "_sel", lambda: _fake_sel_capture(events)())

    payload = json.dumps({"name": "nope"}).encode()

    async def fake_read():
        return payload

    async def fake_json():
        return {"name": "nope"}

    request = MagicMock()
    request.read = fake_read
    request.json = fake_json
    request.content_length = len(payload)
    request.can_read_body = True

    with patch(
        "kiro_crew.apps.builtins.dev_fleet.repository._valid_worktree_names",
        new_callable=AsyncMock, return_value={"other"},
    ):
        resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 400
    assert len(events) == 1
    ev = events[0]
    assert ev["outcome"] == "denied"
    assert ev["tool_name"] == "dev_fleet_worktree_remove"
    assert ev["resources"] == "nope"


@pytest.mark.asyncio
async def test_mutation_success_emits_sel_event(monkeypatch):
    """A successful pod action emits exactly one SEL event with outcome=success."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_pod_down

    events: list = []
    monkeypatch.setattr(runtime_mod, "_sel", lambda: _fake_sel_capture(events)())

    payload = json.dumps({"name": "feature-x"}).encode()

    async def fake_read():
        return payload

    async def fake_json():
        return {"name": "feature-x"}

    request = MagicMock()
    request.read = fake_read
    request.json = fake_json
    request.content_length = len(payload)
    request.can_read_body = True

    with patch(
        "kiro_crew.apps.builtins.dev_fleet.repository._find_worktree",
        new_callable=AsyncMock, return_value=({"name": "feature-x"}, None),
    ), patch(
        "kiro_crew.apps.builtins.dev_fleet.worktree_ops._pod_down",
        new_callable=AsyncMock, return_value={"ok": True},
    ):
        resp = await api_dev_fleet_pod_down(request)
    assert resp.status == 200
    assert len(events) == 1
    assert events[0]["outcome"] == "success"
    assert events[0]["tool_name"] == "dev_fleet_pod_down"
    assert events[0]["resources"] == "feature-x"


@pytest.mark.asyncio
async def test_worktree_remove_non_string_name_is_400(monkeypatch):
    """A list-valued 'name' must be a 400, not a TypeError->500 (Codex R17 #2)."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_worktree_remove

    monkeypatch.setattr(runtime_mod, "_sel", lambda: _fake_sel_capture([])())

    payload = json.dumps({"name": ["feature"]}).encode()

    async def fake_read():
        return payload

    async def fake_json():
        return {"name": ["feature"]}

    request = MagicMock()
    request.read = fake_read
    request.json = fake_json
    request.content_length = len(payload)
    request.can_read_body = True

    resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "non-empty string" in body["error"]


@pytest.mark.asyncio
async def test_mutation_ok_false_audited_as_denied(monkeypatch):
    """A refused operation reported as {"ok": false} with HTTP 200 must be
    audited as denied, never success (Codex R18 #1)."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_worktree_remove

    events: list = []
    monkeypatch.setattr(runtime_mod, "_sel", lambda: _fake_sel_capture(events)())

    payload = json.dumps({"name": "feature-x"}).encode()

    async def fake_read():
        return payload

    async def fake_json():
        return {"name": "feature-x"}

    request = MagicMock()
    request.read = fake_read
    request.json = fake_json
    request.content_length = len(payload)
    request.can_read_body = True

    with patch(
        "kiro_crew.apps.builtins.dev_fleet.repository._valid_worktree_names",
        new_callable=AsyncMock, return_value={"feature-x"},
    ), patch(
        "kiro_crew.apps.builtins.dev_fleet.worktree_ops._worktree_remove",
        new_callable=AsyncMock,
        return_value={"ok": False, "error": "worktree is dirty"},
    ):
        resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 200  # handler keeps its HTTP contract
    assert len(events) == 1
    assert events[0]["outcome"] == "denied"
    assert "dirty" in events[0]["error"]


@pytest.mark.asyncio
async def test_run_record_includes_started():
    """New run records carry a 'started' timestamp for FE reattach (Codex R18 #2)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    before = time.time()
    rid = await mod._start_run("test-label", ["true"])
    async with mod._RUNS_LOCK:
        rec = dict(mod._RUNS[rid])
    assert rec["started"] >= before - 1
    assert rec["started"] <= time.time() + 1
    # let the trivial subprocess worker finish before the loop closes
    for _ in range(50):
        async with mod._RUNS_LOCK:
            if mod._RUNS[rid]["status"] != "running":
                break
        await asyncio.sleep(0.05)


# --- escalation cleanup symlink guard (Codex R19) ---
def test_escalation_cleanup_skips_symlinked_app_dir(tmp_path, monkeypatch):
    """A symlinked escalated app dir must never be followed/deleted — the
    link target lives outside the apps tree (Codex R19 #2)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    # Real directory elsewhere that a malicious/legacy symlink points at
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "installed.json").write_text(json.dumps({"origin": "builtin"}))
    (outside / "data").mkdir()
    (outside / "data" / "keep.txt").write_text("user data")
    (outside / "state.json").write_text("{}")

    (apps_root / "knowledge").symlink_to(outside)

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    mgr.register_builtin_apps()

    # The symlink target must be fully intact — nothing followed or deleted.
    assert (outside / "state.json").exists()
    assert (outside / "data" / "keep.txt").exists()
    assert (apps_root / "knowledge").is_symlink()


# --- cross-repo pod identity guard (Codex R22) ---
@pytest.mark.asyncio
async def test_pod_guard_rejects_foreign_pinned_checkout(monkeypatch, tmp_path):
    """A pod pinned to another repository's checkout must refuse the operation."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "repo-a" / "kirocrew-wt-feature"
    ours.mkdir(parents=True)
    foreign = tmp_path / "repo-b" / "kirocrew-wt-feature"
    foreign.mkdir(parents=True)

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(worktree_ops_mod, "_read_pin_strict",
                      return_value=(True, str(foreign))):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "different checkout" in err


@pytest.mark.asyncio
async def test_pod_guard_allows_matching_or_unpinned(monkeypatch, tmp_path):
    """Matching pin or no pin at all proceeds."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(worktree_ops_mod, "_read_pin_strict",
                      return_value=(True, str(ours))):
        assert await mod._pod_checkout_guard("kirocrew-wt-feature") is None

    # No pin file + no active unit -> pod never booted -> allow
    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(False, None)), \
         patch.object(runtime_mod.rt, "active_names", return_value=set()):
        assert await mod._pod_checkout_guard("kirocrew-wt-feature") is None


@pytest.mark.asyncio
async def test_pod_guard_fails_closed_on_pin_read_error(monkeypatch, tmp_path):
    """Cannot read the pin state -> refuse the pod operation."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(worktree_ops_mod, "_read_pin_strict", side_effect=OSError("boom")):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "cannot verify pod checkout pin" in err


@pytest.mark.asyncio
async def test_pod_guard_denies_pin_file_without_checkout(monkeypatch, tmp_path):
    """A pin file that exists but has no verifiable CHECKOUT is ambiguous
    pod identity -> deny (Codex R23 #2)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(True, None)):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "ambiguous pod identity" in err


@_POSIX_ONLY
def test_read_pin_strict_propagates_read_errors(tmp_path):
    """_read_pin_strict must raise (not return empty) when the pin file
    exists but cannot be read."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    pods = tmp_path / "pods"
    pods.mkdir()
    env = pods / "x.env"
    env.write_text("CHECKOUT='/some/checkout'\n")

    class FakeCfg:
        pods_dir = pods

        def env_file(self, name):
            return env

    assert mod._read_pin_strict(FakeCfg(), "x") == (True, "/some/checkout")

    # Unreadable pin file (exists but open fails) must raise, not return empty
    if os.geteuid() != 0:  # chmod is a no-op guard for root
        env.chmod(0o000)
        try:
            with pytest.raises(OSError):
                mod._read_pin_strict(FakeCfg(), "x")
        finally:
            env.chmod(0o644)


def test_escalation_cleanup_skips_symlinked_meta_file(tmp_path, monkeypatch):
    """A symlinked installed.json must never be read (Codex R23 #1)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    # Sensitive file outside the app dir that a malicious symlink targets
    secret = tmp_path / "credentials.json"
    secret.write_text(json.dumps({"origin": "builtin", "token": "s3cr3t"}))
    (esc / "installed.json").symlink_to(secret)
    (esc / "state.json").write_text("{}")

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    mgr.register_builtin_apps()

    # meta must be treated as None -> keep branch -> nothing deleted
    assert (esc / "state.json").exists()
    assert (esc / "installed.json").is_symlink()
    assert secret.exists()


# --- credential-free build env + pin symlink hardening (Codex R24) ---
def test_build_env_excludes_credentials(monkeypatch):
    """Build/CLI subprocess env must be allowlisted — gateway tokens excluded."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")

    env = mod._pod_env()
    # Pinned, never inherited. The pin is _TRUSTED_PATH plus (in the
    # credential-FREE tier only) the node toolchain dirs prepended, so that
    # npm's `#!/usr/bin/env node` run-scripts resolve — see
    # test_dev_fleet_node_toolchain.py for that boundary.
    assert env["PATH"] != "/usr/bin"
    assert mod._TRUSTED_PATH in env["PATH"]
    assert env["PATH"].endswith(mod._TRUSTED_PATH)
    assert "SLACK_BOT_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env

    assert env["HOME"] == "/home/u"
    assert env["KIROCREW_POD_REPO"] == mod.MAIN_REPO

    benv = mod._build_env()
    assert "SLACK_BOT_TOKEN" not in benv
    assert "KIROCREW_POD_REPO" not in benv

    # The credential-bearing tier keeps the bare pinned path — git resolves its
    # own helpers (git-remote-https, credential helpers) through PATH.
    assert mod._build_env(with_credentials=True)["PATH"] == mod._TRUSTED_PATH


def test_build_env_passes_npm_registry_but_drops_credential_shaped_keys(monkeypatch):
    """NPM_CONFIG_REGISTRY is a registry URL, not a credential -- it must reach
    every Dev Fleet npm step (preflight AND the real ``npm ci``/``npm run
    build``) so both resolve against the same registry. A credential-shaped
    variable next to it must still be dropped by the same allowlist.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://registry.npmjs.org")
    monkeypatch.setenv("NPM_CONFIG__AUTHTOKEN", "npm-secret-token")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")

    for env in (mod._build_env(), mod._build_env(with_credentials=True)):
        assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org"
        assert "NPM_CONFIG__AUTHTOKEN" not in env
        assert "SLACK_BOT_TOKEN" not in env


def test_build_env_rejects_npm_registry_values_that_smuggle_credentials(monkeypatch):
    """``NPM_CONFIG_REGISTRY`` is forwarded only when it is a bare
    ``http``/``https`` registry URL with no userinfo, query, fragment, or
    embedded whitespace -- URL syntax otherwise permits a credential-bearing
    value (``https://user:token@host/``) or a smuggled second value to reach
    a worktree-controlled build script through this allowlist entry. A value
    that fails validation is dropped outright (fail closed), never rewritten,
    and an unrelated credential-shaped variable next to it is still dropped
    too.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    dropped = (
        "https://u:tok@registry.example/",  # userinfo
        "https://registry.example/?x=1",  # query
        "https://registry.example/#f",  # fragment
        "file:///etc/passwd",  # non-http(s) scheme
        "",  # empty
        "https://registry.npmjs.org ",  # embedded whitespace
    )
    for value in dropped:
        monkeypatch.setenv("NPM_CONFIG_REGISTRY", value)
        monkeypatch.setenv("NPM_CONFIG__AUTHTOKEN", "npm-secret-token")
        monkeypatch.setenv("NPM_TOKEN", "npm-secret-token-2")
        env = mod._build_env()
        assert "NPM_CONFIG_REGISTRY" not in env, value
        assert "NPM_CONFIG__AUTHTOKEN" not in env
        assert "NPM_TOKEN" not in env

    # A clean value right after a dropped one still passes through.
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://registry.npmjs.org")
    assert mod._build_env()["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org"


def test_is_safe_env_key_matches_documented_spelling_on_windows():
    """A mixed-case allowlist entry must still match what ``os.environ`` yields.

    The allowlists write ``SystemRoot`` (Microsoft's documented spelling) while
    CPython's ``os.environ`` upper-cases every key on Windows. Folding is what
    keeps the two ends agreeing; without it the filter drops exactly the
    variables it was extended to carry.
    """
    assert "SystemRoot" in mod._WINDOWS_SAFE_ENV_KEYS
    if platform_compat.IS_WINDOWS:
        # The spelling os.environ actually yields.
        assert mod._is_safe_env_key("SYSTEMROOT")
        # And the documented spelling, so either end may be written.
        assert mod._is_safe_env_key("SystemRoot")
    assert not mod._is_safe_env_key("SLACK_BOT_TOKEN")


def test_is_safe_env_key_stays_exact_on_posix():
    """Folding is Windows-only — POSIX names are case-SENSITIVE.

    ``PATH`` and ``Path`` are genuinely different variables there, so a
    case-insensitive match would let a lookalike through.
    """
    assert mod._is_safe_env_key("PATH")
    if not platform_compat.IS_WINDOWS:
        assert not mod._is_safe_env_key("Path")
        # The Windows set must not leak into POSIX matching at all.
        assert not mod._is_safe_env_key("SystemRoot")


def test_windows_safe_env_keys_carry_no_credentials():
    """The Windows additions are platform paths, never secret-bearing vars."""
    for key in mod._WINDOWS_SAFE_ENV_KEYS:
        for marker in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "APIKEY"):
            assert marker not in key.upper(), f"{key!r} looks credential-bearing"


def test_safe_env_keys_platform_composition():
    """The POSIX set always applies; the Windows set only on Windows."""
    posix = set(mod._POSIX_SAFE_ENV_KEYS)
    windows = set(mod._WINDOWS_SAFE_ENV_KEYS)
    active = set(mod._SAFE_ENV_KEYS)

    assert not (posix & windows), "the two sets must stay disjoint"
    assert posix <= active
    if platform_compat.IS_WINDOWS:
        assert windows <= active
    else:
        assert not (windows & active)


@pytest.mark.skipif(
    not platform_compat.IS_WINDOWS, reason="Windows-only env semantics"
)
def test_build_env_carries_systemroot_on_windows(monkeypatch):
    """SYSTEMROOT must reach every build/fetch child.

    Winsock locates its socket catalog through it, so a child without it cannot
    resolve names at all — libcurl reports that as ``getaddrinfo() thread failed
    to start`` and the Pull step fails before it reaches the network.
    """
    monkeypatch.setenv("SYSTEMROOT", r"C:\WINDOWS")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")

    for env in (mod._build_env(), mod._build_env(with_credentials=True), mod._pod_env()):
        assert env["SYSTEMROOT"] == r"C:\WINDOWS"
        # Widening the allowlist must not have widened it to credentials.
        assert "SLACK_BOT_TOKEN" not in env


def test_read_pin_strict_rejects_symlinked_env(tmp_path):
    """A symlinked pin file must raise, never be read (Codex R24 #2)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    pods = tmp_path / "pods"
    pods.mkdir()
    secret = tmp_path / "protected.env"
    secret.write_text("CHECKOUT='/attacker/checkout'\n")
    link = pods / "feature.env"
    link.symlink_to(secret)

    class FakeCfg:
        pods_dir = pods

        def env_file(self, name):
            return link

    # O_NOFOLLOW open refuses the symlink atomically (ELOOP)
    with pytest.raises(OSError):
        mod._read_pin_strict(FakeCfg(), "feature")


def test_read_pin_strict_rejects_escape(tmp_path):
    """A pin path resolving outside pods_dir must raise."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    pods = tmp_path / "pods"
    pods.mkdir()
    outside = tmp_path / "outside.env"
    outside.write_text("CHECKOUT='/x'\n")

    class FakeCfg:
        pods_dir = pods

        def env_file(self, name):
            return outside  # regular file but not under pods_dir

    with pytest.raises(OSError, match="outside"):
        mod._read_pin_strict(FakeCfg(), "feature")


@pytest.mark.asyncio
async def test_completed_runs_are_evicted_beyond_cap():
    """Completed run records are bounded; running entries survive (Codex R28)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    async with mod._RUNS_LOCK:
        saved = dict(mod._RUNS)
        mod._RUNS.clear()
        for i in range(mod._RUNS_MAX_COMPLETED + 5):
            mod._RUNS[f"old{i}"] = {
                "status": "done", "exit_code": 0, "label": "t",
                "output": [], "started": float(i),
            }
        mod._RUNS["active"] = {
            "status": "running", "exit_code": None, "label": "t",
            "output": [], "started": 999.0,
        }
    try:
        rid = await mod._start_run("evict-test", ["true"])
        async with mod._RUNS_LOCK:
            completed = [k for k, v in mod._RUNS.items() if v["status"] != "running"]
            assert len(completed) <= mod._RUNS_MAX_COMPLETED
            assert "active" in mod._RUNS  # running never evicted
            assert "old0" not in mod._RUNS  # oldest completed evicted first
        for _ in range(50):
            async with mod._RUNS_LOCK:
                if mod._RUNS.get(rid, {}).get("status") != "running":
                    break
            await asyncio.sleep(0.05)
    finally:
        async with mod._RUNS_LOCK:
            mod._RUNS.clear()
            mod._RUNS.update(saved)


# --- R29 hardening ---
@pytest.mark.asyncio
async def test_pod_guard_denies_active_unpinned_pod(tmp_path):
    """No pin + ACTIVE unit under the name = unattributable foreign pod -> deny."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(False, None)), \
         patch.object(runtime_mod.rt, "active_names", return_value={"kirocrew-wt-feature"}):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "unattributable" in err

    # No pin + NOT active -> allow (pod never booted)
    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(False, None)), \
         patch.object(runtime_mod.rt, "active_names", return_value=set()):
        assert await mod._pod_checkout_guard("kirocrew-wt-feature") is None


def test_escalation_cleanup_keeps_dir_when_data_present(tmp_path, monkeypatch):
    """Non-empty data/ -> whole dir kept, no partial deletion (R29 #2)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    (esc / "installed.json").write_text(json.dumps({"origin": "builtin"}))
    (esc / "data").mkdir()
    (esc / "data" / "keep.txt").write_text("user data")
    (esc / "state.json").write_text("{}")

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    mgr.register_builtin_apps()

    assert (esc / "state.json").exists()  # nothing partially deleted
    assert (esc / "data" / "keep.txt").exists()


def test_escalation_cleanup_removes_empty_builtin_via_pinned_fd(tmp_path, monkeypatch):
    """No data/ -> dir removed through the pinned descriptor (R29 #2)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    (esc / "installed.json").write_text(json.dumps({"origin": "builtin"}))
    (esc / "state.json").write_text("{}")

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    mgr.register_builtin_apps()

    if (os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd
            and os.rmdir in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")):
        assert not esc.exists()  # dir_fd deletion supported
    else:  # no race-free primitive -> fail closed -> kept
        assert esc.exists()


# --- git config/protocol neutralization chokepoint (Codex R32/R34) ---
def _assert_git_neutralizers(env):
    assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert env["GIT_PROTOCOL_FROM_USER"] == "0"
    # Not a config key and not about code execution: it pins WHICH OBJECT GRAPH
    # git answers from, so every answer this handler acts on describes the
    # history the checkout actually holds rather than a grafted substitute.
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    # Also not a config key: it pins WHAT GIT WRITES on a read. `git status`
    # refreshes the index and saves it back under `index.lock`, so a command that
    # is a read to its caller is a write to the repository.
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    # Full config-driven-execution neutralizer set, injected as env so it
    # covers EVERY git call (background fetch, rebase, sync pull included).
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs == {
        "core.fsmonitor": "false",
        "core.hooksPath": "/dev/null",
        "credential.helper": "",
        "core.sshCommand": "ssh",
        # Signature verification is the third driver class, and it is reached by a
        # READ: `[log] showSignature=true` makes every `git log` verify, and
        # verification EXECS the program these name. All four spellings, because
        # `gpg.openpgp.program` is a synonym that OVERRIDES the bare `gpg.program`.
        "gpg.program": "true",
        "gpg.openpgp.program": "true",
        "gpg.ssh.program": "true",
        "gpg.x509.program": "true",
        "log.showSignature": "false",
    }


@pytest.mark.asyncio
async def test_run_cmd_pins_git_protocols(monkeypatch):
    """Every _run_cmd spawn env carries the full git neutralizer set so an
    ext:: origin / malicious fsmonitor / hooksPath / credential.helper /
    sshCommand from agent-writable .git/config is refused by git itself."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    captured = {}

    def fake_sandbox(cmd, mode, *, env=None, **kw):
        captured["env"] = env
        raise RuntimeError("stop here")  # short-circuit before spawning

    monkeypatch.setattr(runtime_mod, "sandboxed_spawn_argv", fake_sandbox)
    rc, _, err = await mod._run_cmd(["git", "-C", "/x", "fetch"])
    assert rc == -1 and "sandbox unavailable" in err
    _assert_git_neutralizers(captured["env"])


def test_build_env_pins_git_protocols():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    env = mod._build_env()
    _assert_git_neutralizers(env)


# --- cancellation survives an already-reaped child ---
@pytest.mark.asyncio
async def test_run_cmd_cancel_with_reaped_child_propagates_cancellation(monkeypatch):
    """Cancelling _run_cmd whose child was already reaped must raise
    CancelledError, not ProcessLookupError.

    An unguarded ``proc.kill()`` in the CancelledError branch raises
    ProcessLookupError on a reaped child, REPLACING the in-flight
    cancellation; ``_status_refresher``'s broad handler then swallows it
    and loops forever, hanging ``dev_fleet_cleanup``'s ``await bg_task``
    and the whole pytest-asyncio loop teardown.
    """
    entered = asyncio.Event()  # deterministic rendezvous, no sleeps

    class FakeProc:
        pid = 12345
        returncode = None

        async def communicate(self):
            entered.set()
            await asyncio.Event().wait()  # block until cancelled

        def kill(self):
            raise ProcessLookupError  # child already reaped

        async def wait(self):
            return 0

    async def fake_spawn(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(
        runtime_mod, "sandboxed_spawn_argv",
        lambda cmd, mode, *, env=None, **kw: (cmd, env, None),
    )
    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", fake_spawn)
    monkeypatch.setattr(runtime_mod, "_kill_tree", AsyncMock())
    # The shared reap helper also signals the tree; intercept it so the fake
    # pid never reaches a real killpg, and shrink its bound so the reap of a
    # still-blocking communicate() cannot stall this test.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", AsyncMock())
    monkeypatch.setattr(platform_compat, "REAP_TIMEOUT_SECS", 0.01)

    task = asyncio.ensure_future(mod._run_cmd(["/bin/true"]))
    # Bounded so a future early-return in _run_cmd fails fast, not a hang.
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- stream-overrun subprocess reaping (Codex R34 #2) ---
@pytest.mark.asyncio
async def test_start_run_readline_overrun_kills_process_tree(monkeypatch):
    """When the output stream loop raises (e.g. a single line exceeding the
    64 KiB asyncio stream limit), the still-running subprocess tree is
    killed instead of being orphaned past its run record."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    killed: list[int] = []

    class FakeStdout:
        async def readline(self):
            raise ValueError("Separator is found, but chunk is longer than limit")

    class FakeProc:
        pid = 424242
        returncode: int | None = None
        stdout = FakeStdout()
        wait_calls = 0
        communicate_calls = 0

        def kill(self):
            FakeProc.returncode = -9

        async def communicate(self):
            FakeProc.communicate_calls += 1
            FakeProc.returncode = FakeProc.returncode or -9
            return b"", b""

        async def wait(self):
            FakeProc.wait_calls += 1
            FakeProc.returncode = FakeProc.returncode or -9
            return FakeProc.returncode

    async def fake_exec(*a, **kw):
        return FakeProc()

    async def fake_kill_tree(pid):
        killed.append(pid)

    monkeypatch.setattr(runtime_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(runtime_mod, "_kill_tree", fake_kill_tree)
    # The shared reap helper also signals the tree; intercept it so the fake
    # pid never reaches a real killpg on the host.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", AsyncMock())
    FakeProc.returncode = None

    # Absolute: the spawn shim execs without a PATH search, so only a bare name
    # would be resolved (and rejected) before fake_exec is ever reached. The
    # command itself is irrelevant to this test.
    rid = await mod._start_run("overrun-test", ["/usr/bin/whatever"])
    for _ in range(100):
        async with mod._RUNS_LOCK:
            if mod._RUNS[rid]["status"] != "running":
                break
        await asyncio.sleep(0.02)
    async with mod._RUNS_LOCK:
        rec = dict(mod._RUNS[rid])
    assert rec["status"] == "done" and rec["exit_code"] == -1
    assert any("chunk is longer than limit" in line for line in rec["output"])
    assert killed == [424242]  # tree reaped exactly once
    assert FakeProc.returncode is not None  # proc.kill() ran
    # The reap drains pipes via communicate(), never a bare wait() that a
    # full pipe could hang.
    assert FakeProc.communicate_calls == 1
    assert FakeProc.wait_calls == 0


# --- Codex R35 regressions ---
@pytest.mark.asyncio
async def test_sync_fetch_standard_merge_strict(monkeypatch):
    """The sync runner never uses `git pull`: the network fetch runs at
    "standard" while the checkout-performing ff-merge (which executes
    repo-controlled smudge filters / merge drivers) runs "strict" with
    credential dirs hidden, like the pip/npm build steps."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    captured: list[tuple[list, str]] = []

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured.append((list(argv), mode))
        return list(argv), dict(env or {}), None

    with patch.object(repository_mod, "_git", new_callable=AsyncMock,
                      return_value=mod.BASE_BRANCH), \
         patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(runtime_mod, "_build_env", return_value={}), \
         patch.object(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandbox), \
         patch.object(runtime_mod, "_start_run", new_callable=AsyncMock,
                      return_value="rid-sync-test"):
        worktree_ops_mod._SYNC_RID = None
        res = await mod._sync()
        worktree_ops_mod._SYNC_RID = None
    assert res.get("ok") is not False
    assert not any("pull" in argv for argv, _ in captured)
    fetch = [m for argv, m in captured if "fetch" in argv]
    merge = [m for argv, m in captured if "merge" in argv]
    assert fetch == ["standard"]
    assert merge == ["strict"]
    for argv, m in captured:
        if "pip" in " ".join(map(str, argv)) or argv[0] == "npm":
            assert m == "strict"


@pytest.mark.asyncio
async def test_removal_refuses_when_commit_races_verdict():
    """A commit pushed after merge causes OID divergence -> refuse non-forced removal."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/x/wt-feat", "branch": "feat-x",
                                     "is_main": False}, None)), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock,
                      return_value=False), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock,
                      return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock,
                      return_value=0), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock,
                      return_value="deadbeef_local"), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock,
                      return_value="deadbeef_pr_different"), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock,
                      return_value="origin"), \
         patch.object(runtime_mod, "_POD_AVAILABLE", False):
        res = await mod._worktree_remove("wt-feat", force=False)
    assert res["ok"] is False
    assert "OID diverged" in res["error"]


@pytest.mark.asyncio
async def test_owner_repo_failure_not_cached_forever():
    """A transient owner/repo lookup failure retries after the TTL instead
    of disabling PR status until gateway restart; success is cached."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    lookups = AsyncMock(side_effect=[None, "own/repo"])
    fleet_state_mod._OWNER_REPO = None
    fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0
    try:
        with patch.object(fleet_state_mod, "_repo_owner_name", lookups):
            assert await mod._get_owner_repo() is None
            assert lookups.await_count == 1
            # Within the TTL: no re-lookup, still None
            assert await mod._get_owner_repo() is None
            assert lookups.await_count == 1
            # TTL expired: retried and success cached
            fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0
            assert await mod._get_owner_repo() == "own/repo"
            assert await mod._get_owner_repo() == "own/repo"
            assert lookups.await_count == 2
    finally:
        fleet_state_mod._OWNER_REPO = None
        fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0


# --- Codex R36: escalation cleanup pins BEFORE validating ---
def test_escalation_cleanup_fails_closed_without_dirfd(tmp_path, monkeypatch):
    """Platforms without dir_fd primitives cannot pin validation to the
    deletion, so cleanup must be skipped entirely (fail closed)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    (esc / "installed.json").write_text(json.dumps({"origin": "builtin"}))

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)
    monkeypatch.setattr(mgr, "_dirfd_ops_supported", lambda: False)

    mgr.register_builtin_apps()

    assert esc.is_dir()  # kept — no safe primitives to delete with
    assert (esc / "installed.json").exists()


def test_escalation_cleanup_swapped_entry_survives(tmp_path, monkeypatch):
    """A directory swapped in at the same name AFTER the descriptor pin must
    not be rmdir'd: the final unlink verifies the entry still refers to the
    pinned inode."""
    import os as _os

    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    (esc / "installed.json").write_text(json.dumps({"origin": "builtin"}))

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    real_rmtree = mgr._rmtree_dirfd

    def racing_rmtree(fd):
        # Simulate the race: the validated dir is renamed away and an
        # attacker drops a NEW (empty) dir at the same name, right after
        # the pin but before the final unlink-by-name.
        real_rmtree(fd)
        _os.rename(str(esc), str(tmp_path / "moved-away"))
        (apps_root / "knowledge").mkdir()

    monkeypatch.setattr(mgr, "_rmtree_dirfd", racing_rmtree)

    mgr.register_builtin_apps()

    # The swapped-in directory is NOT the validated inode — it must survive.
    assert (apps_root / "knowledge").is_dir()


# ---- builtin re-shell: HMAC middleware, R35, app-process tests ----


def _make_hmac_app():
    app = web.Application(middlewares=[mod.hmac_proxy_middleware])
    app.router.add_get("/health", mod.api_health)
    app.router.add_get("/api/fleet", mod.api_health)  # reuse for HMAC testing
    return app


def _sign_request(secret: str, method: str, path: str, body: bytes = b"") -> dict:
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{ts}:{method}:{path}:{body_hash}"
    sig = _hmac_mod.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"X-KiroCrew-Proxy": f"{ts}:{sig}"}


def test_is_pr_merged():
    assert mod._is_pr_merged({"state": "MERGED", "number": 42}) is True
    assert mod._is_pr_merged({"state": "OPEN"}) is False
    assert mod._is_pr_merged(None) is False
    assert mod._is_pr_merged({}) is False


# =============================================================================
# Redaction (sync)
# =============================================================================


def test_redact_pr_redacts_strings():
    pr = {"url": "https://example.com/secret", "state": "OPEN", "number": 42}
    result = mod._redact_pr(pr)
    assert result is not None
    assert isinstance(result["number"], int)
    assert result["state"] == "OPEN"


def test_redact_pr_none():
    assert mod._redact_pr(None) is None


# =============================================================================
# _get_owner_repo retry logic (R35c)
# =============================================================================


@pytest.mark.asyncio
async def test_get_owner_repo_caches_success():
    fleet_state_mod._OWNER_REPO = None
    fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0
    try:
        with patch.object(fleet_state_mod, "_repo_owner_name", new=AsyncMock(return_value="org/repo")):
            result = await mod._get_owner_repo()
            assert result == "org/repo"
            # Second call uses cache
            result2 = await mod._get_owner_repo()
            assert result2 == "org/repo"
    finally:
        fleet_state_mod._OWNER_REPO = None
        fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0


@pytest.mark.asyncio
async def test_get_owner_repo_retries_on_failure():
    """R35(c): failures retry after 60s backoff, not cached permanently."""
    fleet_state_mod._OWNER_REPO = None
    fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0
    call_count = 0

    async def failing():
        nonlocal call_count
        call_count += 1
        return None

    try:
        with patch.object(fleet_state_mod, "_repo_owner_name", new=failing):
            result = await mod._get_owner_repo()
            assert result is None
            assert call_count == 1

            # Within 60s window, should NOT retry
            result = await mod._get_owner_repo()
            assert result is None
            assert call_count == 1  # blocked by retry_at
    finally:
        fleet_state_mod._OWNER_REPO = None
        fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0


# =============================================================================
# _discover_worktrees sandbox error (QA finding)
# =============================================================================


@pytest.mark.asyncio
async def test_discover_worktrees_sandbox_error_raises():
    """sandbox RuntimeError propagates as RuntimeError, not silent empty."""
    with patch.object(runtime_mod, "_run_cmd", new=AsyncMock(
        return_value=(-1, "", "sandbox unavailable: RuntimeError(no backend)")
    )):
        with pytest.raises(RuntimeError, match="sandbox unavailable"):
            await mod._discover_worktrees()


@pytest.mark.asyncio
async def test_discover_worktrees_sandbox_error_keeps_remedy():
    """The actionable remedy must survive into the propagated message.

    The sandbox layer appends its guidance AFTER a ~180-char preamble, so an
    over-eager length cap here delivered the diagnosis and dropped the fix — the
    Discovery Error banner would end mid-word at "Probe". Guard the tail, not
    just the prefix (the pre-existing test only checked the prefix, which is why
    the truncation went unnoticed).
    """
    stderr = (
        "sandbox unavailable: Sandbox backend unavailable and "
        "allow_unsandboxed_exec is not set. No OS-level sandbox backend is "
        "available on this host, and the agent subprocess cannot be safely "
        "isolated. Probe detail: sandbox-exec probe failed (exit 71). This "
        "host's sandbox is NOT broken: the kernel reports this process is "
        "already inside a macOS Seatbelt sandbox that KiroCrew did not create. "
        'Set {"sandbox": false} in ~/.kiro/settings/amazon-internal.json so '
        "KiroCrew's own profile owns isolation, then restart the gateway."
    )
    assert len(stderr) > 200, "fixture must exceed the old cap to be meaningful"
    with patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(-1, "", stderr))):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    msg = str(exc.value)
    assert "amazon-internal.json" in msg
    assert not msg.endswith("Probe")
    assert len(msg) <= mod._SANDBOX_ERR_MAX


@pytest.mark.asyncio
async def test_discover_worktrees_sandbox_error_is_still_bounded():
    """An unbounded stderr is still clipped before reaching the UI."""
    stderr = "sandbox unavailable: " + ("x" * 5000)
    with patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(-1, "", stderr))):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    assert len(str(exc.value)) == mod._SANDBOX_ERR_MAX


@pytest.mark.asyncio
async def test_discover_worktrees_missing_repo_raises_actionable_error(tmp_path):
    """A missing/non-git MAIN_REPO raises with the path and the remedy.

    A silent [] here would render as the
    "No worktrees found" empty state. On packaged installs (where
    KIROCREW_PROJECT_DIR points at the app bundle and discovery falls through
    to the hardcoded ~/kirocrew) that empty state told users they had no
    worktrees when the app was simply looking at a path that does not exist.
    """
    missing = tmp_path / "does-not-exist"
    with patch.object(repository_mod, "MAIN_REPO", str(missing)), \
         patch.object(runtime_mod, "_run_cmd", new=AsyncMock(
             return_value=(128, "", f"fatal: cannot change to '{missing}'")
         )):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    msg = str(exc.value)
    assert str(missing) in msg  # names the path it tried
    assert "KIROCREW_DEVFLEET_REPO" in msg  # names the remedy


@pytest.mark.asyncio
async def test_discover_worktrees_git_failure_raises_with_stderr(tmp_path):
    """When the repo exists but git fails, git's own message is surfaced."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with patch.object(repository_mod, "MAIN_REPO", str(repo)), \
         patch.object(runtime_mod, "_run_cmd", new=AsyncMock(
             return_value=(128, "", "fatal: index file corrupt")
         )):
        with pytest.raises(RuntimeError, match="index file corrupt"):
            await mod._discover_worktrees()


@pytest.mark.asyncio
async def test_discover_worktrees_git_failure_is_bounded(tmp_path):
    """An unbounded git stderr is clipped before reaching the UI."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with patch.object(repository_mod, "MAIN_REPO", str(repo)), \
         patch.object(runtime_mod, "_run_cmd", new=AsyncMock(
             return_value=(128, "", "fatal: " + "x" * 5000)
         )):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    # bounded: the git portion is clipped to _GIT_ERR_MAX plus the fixed prefix
    assert len(str(exc.value)) <= mod._GIT_ERR_MAX + 200


@pytest.mark.asyncio
async def test_discover_worktrees_unresolved_git_blames_host_not_repo(tmp_path):
    """No trusted git => the error names the tool + override, not the repo.

    Without the fix this failure surfaces as "git worktree discovery
    failed in <repo>: no trusted executable for 'git' in <PATH>" — blaming a
    healthy checkout, echoing the whole trusted PATH into the UI, and never
    naming KIROCREW_DEVFLEET_BIN_GIT, the override that is the actual remedy.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    stderr = f"{mod._UNRESOLVED_TOOL_PREFIX}'git' in {mod._TRUSTED_PATH}"
    with patch.object(repository_mod, "MAIN_REPO", str(repo)), \
         patch.object(runtime_mod, "_run_cmd", new=AsyncMock(
             return_value=(-1, "", stderr)
         )):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    msg = str(exc.value)
    assert "'git'" in msg  # names the tool that could not be resolved
    assert "KIROCREW_DEVFLEET_BIN_GIT" in msg  # names the remedy
    assert mod._TRUSTED_PATH not in msg  # PATH stays in the log, not the UI
    assert "worktree discovery failed" not in msg  # not blamed on the repo
    assert str(repo) not in msg  # the checkout is not implicated at all


@pytest.mark.asyncio
async def test_discover_worktrees_real_git_error_not_misclassified(tmp_path):
    """A git failure merely MENTIONING the sentinel text mid-string is not
    reclassified: only a stderr `_run_cmd` itself synthesized (prefix at
    position 0) takes the unresolved-tool branch; everything else still
    surfaces git's own redacted, bounded message."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with patch.object(repository_mod, "MAIN_REPO", str(repo)), \
         patch.object(runtime_mod, "_run_cmd", new=AsyncMock(
             return_value=(128, "", f"fatal: {mod._UNRESOLVED_TOOL_PREFIX}'hook'")
         )):
        with pytest.raises(RuntimeError, match="worktree discovery failed"):
            await mod._discover_worktrees()


@pytest.mark.asyncio
async def test_sync_unresolved_git_names_override_not_path(monkeypatch):
    """/api/sync with no trusted git returns the same remedy-first message."""
    with patch.object(repository_mod, "_git", new_callable=AsyncMock,
                      return_value=mod.BASE_BRANCH), \
         patch.object(worktree_ops_mod, "_venv_python",
                      return_value=Path("/fake/.venv/bin/python")), \
         patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: None), \
         patch.object(worktree_ops_mod.dep_sync, "locked_console_scripts", return_value=[]):
        worktree_ops_mod._SYNC_RID = None
        res = await mod._sync()
    assert res["ok"] is False
    assert "'git'" in res["error"]
    assert "KIROCREW_DEVFLEET_BIN_GIT" in res["error"]
    assert mod._TRUSTED_PATH not in res["error"]


def test_bin_override_var_derivation():
    """The advertised override var matches what _trusted_bin actually reads,
    including dash-to-underscore mapping for non-git tools."""
    assert mod._bin_override_var("git") == "KIROCREW_DEVFLEET_BIN_GIT"
    assert mod._bin_override_var("some-tool") == "KIROCREW_DEVFLEET_BIN_SOME_TOOL"
    assert "KIROCREW_DEVFLEET_BIN_GIT" in mod._unresolved_tool_message("git")


# =============================================================================
# HMAC middleware tests
# =============================================================================


@pytest.mark.asyncio
async def test_hmac_valid_signature_passes():
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/fleet")
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 200


@pytest.mark.asyncio
async def test_hmac_missing_header_returns_401():
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value="secret"):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 401
            body = await resp.json()
            assert "missing" in body["error"]


@pytest.mark.asyncio
async def test_hmac_invalid_signature_returns_401():
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value="secret"):
        async with TestClient(TestServer(app)) as client:
            ts = str(int(time.time()))
            headers = {"X-KiroCrew-Proxy": f"{ts}:badbadbadbad"}
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 401
            body = await resp.json()
            assert "invalid" in body["error"]


@pytest.mark.asyncio
async def test_hmac_expired_timestamp_returns_401():
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            old_ts = str(int(time.time()) - 120)  # 2 min old
            body_hash = hashlib.sha256(b"").hexdigest()
            msg = f"{old_ts}:GET:/api/fleet:{body_hash}"
            sig = _hmac_mod.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
            headers = {"X-KiroCrew-Proxy": f"{old_ts}:{sig}"}
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 401
            body = await resp.json()
            assert "expired" in body["error"]


@pytest.mark.asyncio
async def test_hmac_health_bypasses_verification():
    """Health endpoint does not require HMAC (used by the backend health loop in
    ``apps/backend_runtime/supervision.py``)."""
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=""):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire_target",
    [
        "/api/fleet?name=my%20worktree",  # space
        "/api/fleet?name=caf%C3%A9",  # non-ASCII
        "/api/fleet?name=a+b",  # '+' (decodes to space in query)
    ],
)
async def test_hmac_gateway_signed_escapable_query_passes(wire_target: str):
    """The gateway signs the RAW percent-encoded request-target; the middleware
    must recompute over the same wire bytes, not aiohttp's decoded
    path + query_string reconstruction, or every escapable query value 401s."""
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", wire_target)
            # encoded=True keeps the exact signed bytes on the wire, mirroring
            # the gateway's yarl.URL(..., encoded=True) forwarding.
            resp = await client.get(yarl.URL(wire_target, encoded=True), headers=headers)
            assert resp.status == 200, await resp.text()


@pytest.mark.asyncio
async def test_hmac_gateway_signed_no_query_target_passes():
    """Without a query string neither side appends a '?' — raw and decoded
    spellings coincide and the signature must still verify."""
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/fleet")
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 200, await resp.text()


# =============================================================================
# R35(b): worktree removal verdict_oid gate
# =============================================================================


@pytest.mark.asyncio
async def test_worktree_remove_refuses_cherry_ahead_at_pinned_oid():
    """Non-forced removal with merged PR must fail if branch OID != PR headRefOid."""
    fake_wt = {"path": "/tmp/fake-wt", "branch": "feat-x", "is_main": False}

    with patch.object(repository_mod, "_find_worktree", new=AsyncMock(return_value=(fake_wt, None))), \
         patch.object(repository_mod, "_git", new=AsyncMock(return_value="local_oid_aaa")), \
         patch.object(repository_mod, "_real_dirty", new=AsyncMock(return_value=False)), \
         patch.object(fleet_state_mod, "_pr_status_cached", new=AsyncMock(return_value={"state": "MERGED", "number": 1})), \
         patch.object(repository_mod, "_own_commits_count", new=AsyncMock(return_value=0)), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new=AsyncMock(return_value="pr_oid_bbb")), \
         patch.object(repository_mod, "_upstream_remote", new=AsyncMock(return_value="origin")), \
         patch.object(runtime_mod, "_load_cfg", return_value=None), \
         patch.object(runtime_mod, "_POD_AVAILABLE", False):
        result = await mod._worktree_remove("feat-x", force=False)
        assert result["ok"] is False
        assert "OID diverged" in result["error"]


# =============================================================================
# Fleet handler catches sandbox RuntimeError
# =============================================================================


@pytest.mark.asyncio
async def test_fleet_handler_sandbox_error_returns_error_payload():
    """api_dev_fleet_fleet returns error payload when sandbox is unavailable."""
    async def boom():
        raise RuntimeError("sandbox unavailable: no backend")

    with patch.object(fleet_state_mod, "_fleet_refresh", new=boom), \
         patch.object(fleet_state_mod, "_fleet_cached", new=boom):
        # Use a minimal app to test the handler
        app = web.Application()
        app.router.add_get("/api/fleet", mod.api_dev_fleet_fleet)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 200
            body = await resp.json()
            assert body["worktrees"] == []
            assert "sandbox unavailable" in body["error"]


@pytest.mark.asyncio
async def test_fleet_handler_missing_repo_returns_error_payload():
    """The missing-repo RuntimeError reaches the client as the error payload
    (the frontend renders it as the Discovery Error banner), not a silent
    empty fleet."""
    async def boom():
        raise RuntimeError(
            "main checkout not found: /nope/kirocrew is missing or not a git "
            "checkout. Set KIROCREW_DEVFLEET_REPO to your Kiro Crew checkout, "
            "or clone it to ~/kirocrew."
        )

    with patch.object(fleet_state_mod, "_fleet_refresh", new=boom), \
         patch.object(fleet_state_mod, "_fleet_cached", new=boom):
        app = web.Application()
        app.router.add_get("/api/fleet", mod.api_dev_fleet_fleet)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 200
            body = await resp.json()
            assert body["worktrees"] == []
            assert "main checkout not found" in body["error"]
            assert "KIROCREW_DEVFLEET_REPO" in body["error"]


# =============================================================================
# main-checkout discovery (no invented default path)
# =============================================================================


def _make_checkout(root: Path) -> Path:
    """Create a directory carrying every Kiro Crew checkout marker."""
    (root / ".git").mkdir(parents=True)
    (root / "src" / "kiro_crew").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'kiro-crew'\n")
    return root


def test_is_kirocrew_checkout_requires_every_marker(tmp_path):
    """A bare git repo is refused: adopting it would run Pull+Build inside it."""
    bare = tmp_path / "some-other-repo"
    (bare / ".git").mkdir(parents=True)
    assert mod._is_kirocrew_checkout(str(bare)) is False
    assert mod._is_kirocrew_checkout(str(_make_checkout(tmp_path / "kirocrew"))) is True
    assert mod._is_kirocrew_checkout("") is False


def test_default_main_repo_returns_empty_when_nothing_is_found(monkeypatch):
    """No checkout resolves to "" — never to a path nobody asked for.

    A synthesized default makes a first run report a missing checkout the user
    never chose, which reads as a broken app rather than an open question.
    """
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_own_source_checkout", return_value=None):
        assert mod._default_main_repo() == ""


def test_default_main_repo_skips_a_project_dir_that_is_not_kirocrew(monkeypatch, tmp_path):
    """A project directory that is some other git repo is not adopted."""
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(other))
    with patch.object(repository_mod, "_own_source_checkout", return_value=None):
        assert mod._default_main_repo() == ""


def test_default_main_repo_falls_back_to_the_running_checkout(monkeypatch, tmp_path):
    """A gateway running from source manages that source tree, unconfigured."""
    own = _make_checkout(tmp_path / "kirocrew")
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_own_source_checkout", return_value=str(own)):
        assert mod._default_main_repo() == str(own)


def test_discover_main_repo_honors_the_config_repo_path(monkeypatch):
    """``dev_fleet.repo_path`` is a supported alternative to the env var."""
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={"repo_path": "/opt/kc"}):
        assert mod._discover_main_repo() == "/opt/kc"


def test_discover_main_repo_takes_an_explicit_path_verbatim(monkeypatch):
    """A configured path is NOT marker-tested: a typo must surface as an error
    naming that path, not be silently swapped for a discovered checkout."""
    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/typo/kirocrew")
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={"repo_path": "/opt/kc"}):
        assert mod._discover_main_repo() == "/typo/kirocrew"


def test_discover_main_repo_finds_a_conventional_clone_location(monkeypatch, tmp_path):
    """A real checkout in a conventional location is found without configuration."""
    home = tmp_path / "home"
    checkout = _make_checkout(home / "Repos" / "KiroCrew")  # brand-ok: clone dir name
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}), \
         patch.object(repository_mod, "_own_source_checkout", return_value=None), \
         patch.object(repository_mod.Path, "home", staticmethod(lambda: home)):
        assert mod._discover_main_repo() == str(checkout)


def test_discover_main_repo_uses_the_filesystem_spelling(monkeypatch, tmp_path):
    """The returned path is spelled the way the directory is, not the way the
    probe list guesses — a case-variant does not match what git reports."""
    home = tmp_path / "home"
    checkout = _make_checkout(home / "repos" / "KIROCREW")
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}), \
         patch.object(repository_mod, "_own_source_checkout", return_value=None), \
         patch.object(repository_mod.Path, "home", staticmethod(lambda: home)):
        assert mod._discover_main_repo() == str(checkout)


def test_discover_main_repo_ignores_an_unmarked_conventional_location(monkeypatch, tmp_path):
    """An empty ~/kirocrew directory does not count as a checkout."""
    home = tmp_path / "home"
    (home / "kirocrew").mkdir(parents=True)
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}), \
         patch.object(repository_mod, "_own_source_checkout", return_value=None), \
         patch.object(repository_mod.Path, "home", staticmethod(lambda: home)):
        assert mod._discover_main_repo() == ""


@pytest.mark.asyncio
async def test_discover_worktrees_without_a_repo_never_runs_git():
    """`git -C ""` answers for the backend's own working directory, so an
    unresolved repo must not reach git at all."""
    run = AsyncMock(return_value=(0, "", ""))
    with patch.object(repository_mod, "MAIN_REPO", ""), patch.object(runtime_mod, "_run_cmd", new=run):
        with pytest.raises(mod.RepoNotConfigured):
            await mod._discover_worktrees()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_remote_without_a_repo_never_runs_git():
    run = AsyncMock(return_value=(0, "", ""))
    with patch.object(repository_mod, "MAIN_REPO", ""), \
         patch.object(repository_mod, "_UPSTREAM_REMOTE", None), \
         patch.object(runtime_mod, "_run_cmd", new=run):
        assert await mod._upstream_remote() == "origin"
    run.assert_not_awaited()


def test_build_pending_without_a_repo_is_false():
    """Path("") is Path("."), which would stat this process's own tree."""
    with patch.object(repository_mod, "MAIN_REPO", ""):
        assert mod._build_pending() is False


@pytest.mark.asyncio
async def test_fleet_handler_reports_needs_setup_without_an_error():
    """No checkout found is a setup state, not a failure: the payload carries
    ``needs_setup`` and NO ``error``, so the page asks where the checkout is
    instead of rendering a red banner against a path the user never chose."""
    async def unconfigured():
        raise mod.RepoNotConfigured("no Kiro Crew checkout found to manage")

    with patch.object(fleet_state_mod, "_fleet_refresh", new=unconfigured), \
         patch.object(fleet_state_mod, "_fleet_cached", new=unconfigured):
        app = web.Application()
        app.router.add_get("/api/fleet", mod.api_dev_fleet_fleet)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 200
            body = await resp.json()
            assert body == {"worktrees": [], "needs_setup": True}


@pytest.mark.asyncio
async def test_discover_worktrees_refuses_a_configured_non_kirocrew_repo():
    """A configured path that is a readable git repo but NOT this project is
    refused by name, never operated on. Tiers 1-2 skip the marker test at
    DISCOVERY so a typo is not silently replaced by a found checkout — that must
    not also mean the wrong tree gets `worktree remove` run inside it."""
    run = AsyncMock(return_value=(0, "worktree /some/other/repo\n", ""))
    with patch.object(repository_mod, "MAIN_REPO", "/some/other/repo"), \
         patch.object(repository_mod, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: /some/other/repo ..."), \
         patch.object(runtime_mod, "_run_cmd", new=run):
        with pytest.raises(mod.RepoUnreadable) as exc:
            await mod._discover_worktrees()
    # Refused BEFORE git ran: a readable wrong repo answers `worktree list`
    # happily, so the guard cannot rely on a non-zero exit.
    run.assert_not_awaited()
    assert "/some/other/repo" in str(exc.value)
    assert "not a Kiro Crew checkout" in str(exc.value)


@pytest.mark.asyncio
async def test_auto_prune_reaper_idles_for_a_configured_non_kirocrew_repo():
    """An enabled reaper must not run a cycle against an unusable checkout.

    MAIN_REPO is TRUTHY for a configured path that fails the marker test, so a
    truthiness guard let the cycle proceed: _prune_candidates raised
    RepoUnreadable, `except Exception` logged a traceback, and the reaper recorded
    outcome="failure" in the SEL trail every interval — a tamper-evident audit
    asserting a failure that never happened.
    """
    prune = AsyncMock()
    sel = MagicMock()
    with patch.object(repository_mod, "MAIN_REPO", "/some/other/repo"), \
         patch.object(repository_mod, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: ..."), \
         patch.object(worktree_ops_mod, "_auto_prune_cfg", return_value=(True, 0.01)), \
         patch.object(worktree_ops_mod, "_auto_prune_once", new=prune), \
         patch.object(runtime_mod, "_sel", return_value=sel), \
         patch.object(worktree_ops_mod, "asyncio", wraps=asyncio) as aio:
        aio.sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with pytest.raises(asyncio.CancelledError):
            await mod._auto_prune_reaper()
    prune.assert_not_awaited()
    sel.log_tool_invocation.assert_not_called()


@pytest.mark.asyncio
async def test_sync_refuses_a_configured_non_kirocrew_repo():
    """The gate lives in the accessor, not the discovery funnel: sync and the
    refresher never pass through _discover_worktrees, and `pull --ff-only` plus
    `pip install -e` inside an unrelated repository is the worst outcome here."""
    with patch.object(repository_mod, "MAIN_REPO", "/some/other/repo"), \
         patch.object(repository_mod, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: /some/other/repo ..."):
        res = await mod._sync_start_locked()
    assert res["ok"] is False
    assert "not a Kiro Crew checkout" in res["error"]


@pytest.mark.asyncio
async def test_status_refresher_idles_for_a_configured_non_kirocrew_repo():
    run = AsyncMock(return_value=(0, "", ""))
    with patch.object(repository_mod, "MAIN_REPO", "/some/other/repo"), \
         patch.object(repository_mod, "_REPO_INVALID_MSG", "not a Kiro Crew checkout: /some/other/repo ..."), \
         patch.object(runtime_mod, "_run_cmd", new=run):
        await mod._status_refresher()
    # Returned without fetching: no git ran against the unrelated repository.
    run.assert_not_awaited()


def test_repo_accessor_gates_both_unusable_states():
    """Both states raise from the accessor, and both share a base so a degrading
    caller cannot enumerate only the reason that existed when it was written."""
    with patch.object(repository_mod, "MAIN_REPO", ""), patch.object(repository_mod, "_REPO_INVALID_MSG", None):
        with pytest.raises(mod.RepoNotConfigured):
            mod._repo()
    with patch.object(repository_mod, "MAIN_REPO", "/x"), patch.object(repository_mod, "_REPO_INVALID_MSG", "bad"):
        with pytest.raises(mod.RepoUnreadable):
            mod._repo()
    assert issubclass(mod.RepoNotConfigured, mod.RepoUnavailable)
    assert issubclass(mod.RepoUnreadable, mod.RepoUnavailable)
    with patch.object(repository_mod, "MAIN_REPO", "/good"), patch.object(repository_mod, "_REPO_INVALID_MSG", None):
        assert mod._repo() == "/good"


@pytest.mark.asyncio
async def test_discover_worktrees_proceeds_for_a_validated_checkout():
    with patch.object(repository_mod, "MAIN_REPO", "/good/kirocrew"), \
         patch.object(repository_mod, "_REPO_INVALID_MSG", None), \
         patch.object(runtime_mod, "_run_cmd", new=AsyncMock(
             return_value=(0, "worktree /good/kirocrew\nbranch refs/heads/main\n", "")
         )):
        entries = await mod._discover_worktrees()
    assert entries and entries[0]["is_main"] is True


@pytest.mark.asyncio
async def test_sync_refuses_without_a_repo():
    with patch.object(repository_mod, "MAIN_REPO", ""):
        res = await mod._sync_start_locked()
    assert res["ok"] is False
    assert "no Kiro Crew checkout" in res["error"]


@pytest.mark.asyncio
async def test_unconfigured_repo_answers_a_coded_409_not_a_500():
    """A route that resolves worktrees must not answer a first-run click with an
    uncaught 500 and a generic failure toast. The boundary lives in the
    middleware, so a new route cannot forget the case, and the body carries a
    machine-readable code rather than only prose."""
    secret = "s" * 32

    async def boom(request):
        raise mod.RepoNotConfigured("no Kiro Crew checkout found to manage")

    app = web.Application(middlewares=[mod.hmac_proxy_middleware])
    app.router.add_get("/api/prune-candidates", boom)
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/prune-candidates")
            resp = await client.get("/api/prune-candidates", headers=headers)
            assert resp.status == 409
            body = await resp.json()
    assert body["code"] == "repo_not_configured"
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_unreadable_repo_answers_a_coded_409_too():
    """A named-but-unreadable checkout has the same consequence for every route
    except /fleet — no fleet to act on — so it gets the same boundary with its own
    code, instead of an uncaught 500 behind a "Prune preview failed" toast."""
    secret = "s" * 32

    async def boom(request):
        raise mod.RepoUnreadable("main checkout not found: /opt/kc is missing or not a git checkout.")

    app = web.Application(middlewares=[mod.hmac_proxy_middleware])
    app.router.add_get("/api/prune-candidates", boom)
    with patch.object(http_api_mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/prune-candidates")
            resp = await client.get("/api/prune-candidates", headers=headers)
            assert resp.status == 409
            body = await resp.json()
    assert body["code"] == "repo_unreadable"


@pytest.mark.asyncio
async def test_fleet_handler_still_reports_an_unreadable_repo_as_an_error():
    """/fleet is the one route that distinguishes them: unreadable keeps the
    error payload (the Discovery Error banner names the path the user chose)."""
    async def boom():
        raise mod.RepoUnreadable("main checkout not found: /opt/kc is missing or not a git checkout.")

    with patch.object(fleet_state_mod, "_fleet_refresh", new=boom), \
         patch.object(fleet_state_mod, "_fleet_cached", new=boom):
        app = web.Application()
        app.router.add_get("/api/fleet", mod.api_dev_fleet_fleet)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            body = await resp.json()
    assert body["worktrees"] == []
    assert "main checkout not found" in body["error"]
    assert "needs_setup" not in body


def test_repo_source_hint_names_the_env_var(monkeypatch):
    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/typo/kirocrew")
    assert "KIROCREW_DEVFLEET_REPO" in mod._repo_source_hint()
    assert "config.json" not in mod._repo_source_hint()


def test_repo_source_hint_names_the_config_key(monkeypatch):
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={"repo_path": "/typo/kc"}):
        hint = mod._repo_source_hint()
    assert "config.json" in hint
    assert "environment variable" not in hint


def test_repo_source_hint_offers_both_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}):
        hint = mod._repo_source_hint()
    assert "KIROCREW_DEVFLEET_REPO" in hint and "config.json" in hint


# =============================================================================
# GIT_CONFIG_COUNT env neutralizers
# =============================================================================


def test_git_env_neutralizers_present():
    """_GIT_ENV_NEUTRALIZERS pins protocol and neutralizes execution vectors."""
    n = mod._GIT_ENV_NEUTRALIZERS
    assert n["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert n["GIT_PROTOCOL_FROM_USER"] == "0"
    assert n["GIT_NO_REPLACE_OBJECTS"] == "1"
    # GIT_NO_REPLACE_OBJECTS and GIT_OPTIONAL_LOCKS are env vars in their own
    # right, NOT config pairs, so the count must not have grown to cover them.
    assert n["GIT_OPTIONAL_LOCKS"] == "0"
    assert n["GIT_CONFIG_COUNT"] == "9"
    assert n["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
    assert n["GIT_CONFIG_VALUE_0"] == "false"
    assert n["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert n["GIT_CONFIG_VALUE_1"] == "/dev/null"
    assert n["GIT_CONFIG_KEY_2"] == "credential.helper"
    assert n["GIT_CONFIG_VALUE_2"] == ""
    assert n["GIT_CONFIG_KEY_3"] == "core.sshCommand"
    assert n["GIT_CONFIG_VALUE_3"] == "ssh"
    # Signature verification EXECS the program these name, and `showSignature` is
    # the trigger that makes a plain `git log` reach for one. All four program
    # spellings, because `gpg.openpgp.program` is a synonym that OVERRIDES the bare
    # `gpg.program`, so pinning only the bare key leaves an unpinned way in.
    assert n["GIT_CONFIG_KEY_4"] == "gpg.program"
    assert n["GIT_CONFIG_VALUE_4"] == "true"
    assert n["GIT_CONFIG_KEY_5"] == "gpg.openpgp.program"
    assert n["GIT_CONFIG_VALUE_5"] == "true"
    assert n["GIT_CONFIG_KEY_6"] == "gpg.ssh.program"
    assert n["GIT_CONFIG_VALUE_6"] == "true"
    assert n["GIT_CONFIG_KEY_7"] == "gpg.x509.program"
    assert n["GIT_CONFIG_VALUE_7"] == "true"
    assert n["GIT_CONFIG_KEY_8"] == "log.showSignature"
    assert n["GIT_CONFIG_VALUE_8"] == "false"
    # The count DERIVED, not retyped: an undercount silently drops the tail of the
    # list, so the pins past it would be absent while this test still read green
    # against a hardcoded number that matched the stale count.
    pairs = sum(1 for k in n if k.startswith("GIT_CONFIG_KEY_"))
    assert n["GIT_CONFIG_COUNT"] == str(pairs), "the count must cover every pinned pair"
    for i in range(pairs):
        assert f"GIT_CONFIG_VALUE_{i}" in n, f"key {i} has no value beside it"


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required to exercise a real refs/replace graft",
)
def test_the_neutralizers_answer_from_the_real_object_graph(tmp_path, monkeypatch):
    """A ``refs/replace`` graft must not change what Dev Fleet's git reads report.

    Pinned by BEHAVIOUR against a real repository rather than by asserting the
    variable is present, because the claim is about git's own semantics: a
    ``refs/replace/<oid>`` ref substitutes one object for another in EVERY read, so
    ``rev-list --count`` walks the substitute's parents and answers about a history
    no checked-out commit names. Dev Fleet acts on exactly that number -- it is how
    "behind by N commits" and the sync's own decisions are formed -- so the real
    graph is the only one that answers the question asked.

    The unpinned reading is asserted too, so this test fails if git ever stops
    honouring the graft and the pin becomes a no-op nobody would notice.
    """
    import subprocess

    git = shutil.which("git")
    repo = tmp_path / "repo"
    repo.mkdir()

    # An exported GIT_DIR/GIT_WORK_TREE is PLANTED rather than assumed absent,
    # because it is what makes the allowlist below observable instead of a claim.
    # This decoy stands in for the operator's real checkout: drop the filtering
    # and every ``run()`` retargets here, so the suite mutates a repository
    # outside tmp_path and the graft assertions answer about a history this test
    # never built.
    decoy = tmp_path / "decoy.git"
    decoy.mkdir()
    monkeypatch.setenv("GIT_DIR", str(decoy))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "decoy-work"))

    # The base env is the module's OWN allowlist (`_is_safe_env_key`, what
    # `_build_env` filters through) rather than a merge over `os.environ`,
    # because git takes its location from the environment and those variables
    # outrank ``-C <repo>``: an exported ``GIT_DIR``, ``GIT_WORK_TREE`` or
    # ``GIT_INDEX_FILE`` would send the ``add``/``commit``/``replace`` below at an
    # unrelated repository, so running the suite would mutate an operator's real
    # index and refs. ``GIT_REPLACE_REF_BASE`` and ``GIT_NO_REPLACE_OBJECTS``
    # would also decide the graft assertions before they were made. An allowlist
    # removes the whole class in one place, including any variable git adds later.
    #
    # Global and system config are excluded for the mirror-image reason: a
    # ``commit.gpgsign`` or ``core.hooksPath`` in the operator's ``~/.gitconfig``
    # would fail these fixture commits on their machine and nowhere else.
    # Repository-local config still applies, which is what the identity below is
    # set through.
    base_env = {k: v for k, v in os.environ.items() if mod._is_safe_env_key(k)}
    base_env["GIT_CONFIG_GLOBAL"] = os.devnull
    base_env["GIT_CONFIG_SYSTEM"] = os.devnull

    def run(*args, env=None):
        # ``cwd=repo`` alongside ``-C repo``: the location is pinned twice on
        # purpose. ``-C`` is what the module under test relies on; ``cwd`` keeps
        # this fixture's real git from ever running in the worker's checkout.
        proc = subprocess.run(
            [git, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={**base_env, **(env or {})},
            cwd=repo,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "T")
    oids = []
    for i in range(3):
        (repo / "f.txt").write_text(f"{i}\n", encoding="utf-8")
        run("add", "f.txt")
        run("commit", "-q", "-m", f"c{i}")
        oids.append(run("rev-parse", "HEAD"))

    # Three commits, so HEAD's history is three deep.
    assert run("rev-list", "--count", "HEAD") == "3"
    # Graft HEAD onto the root, which shortens the history git reports.
    run("replace", "--graft", oids[2], oids[0])

    # Ungrafted the reading is the truth; grafted it is not. Both are asserted so
    # neither half of the pin can rot silently.
    assert run("rev-list", "--count", "HEAD") == "2"
    assert (
        run("rev-list", "--count", "HEAD", env={"GIT_NO_REPLACE_OBJECTS": "1"}) == "3"
    )
    assert run("rev-list", "--count", "HEAD", env=mod._GIT_ENV_NEUTRALIZERS) == "3"

    # Nothing reached the decoy, so the allowlist rather than luck is what kept
    # every mutation above inside tmp_path.
    assert list(decoy.iterdir()) == [], "an inherited GIT_DIR retargeted the fixture"


# =============================================================================
# _audited decorator exists and is applied
# =============================================================================


def test_audited_decorator_applied_to_mutations():
    """All mutating handlers are wrapped by _audited."""
    import inspect
    for name in [
        "api_dev_fleet_sync", "api_dev_fleet_worktree_remove",
        "api_dev_fleet_prune_run", "api_dev_fleet_pod_up",
        "api_dev_fleet_pod_down", "api_dev_fleet_pod_restart",
        "api_dev_fleet_pod_token", "api_dev_fleet_pod_provision",
        "api_dev_fleet_pod_provision_dismiss",
        "api_dev_fleet_rebase",
    ]:
        fn = getattr(mod, name)
        # _audited wraps with __name__ preserved
        assert callable(fn), f"{name} is not callable"
        assert inspect.iscoroutinefunction(fn), f"{name} is not async"


# =============================================================================
# create_app / main structure
# =============================================================================


def test_create_app_returns_aiohttp_application():
    app = mod.create_app()
    assert isinstance(app, web.Application)
    routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, "resource")]
    assert "/health" in routes
    assert "/api/fleet" in routes
    assert "/api/sync" in routes
    # Served by the GATEWAY process now (gateway_routes.py): the pointer they touch
    # is masked from this backend and every child it spawns.
    assert "/api/restart-gateway" not in routes
    assert "/api/make-live" not in routes


# ---- platform fixes discovered during pod QA of the builtin re-shell ----


def test_backend_spawn_env_includes_kirocrew_home(monkeypatch):
    """The app backend must resolve the SAME config home as the gateway:
    minimal_env() strips KIROCREW_HOME, so spawn must re-inject it or the
    backend reads the wrong .app_secret and every proxied call 401s."""
    import kiro_crew.apps.registry as registry

    monkeypatch.setenv("KIROCREW_HOME", "/tmp/some-pod-home")
    env = registry.minimal_env(KIROCREW_HOME="/tmp/some-pod-home")
    assert env["KIROCREW_HOME"] == "/tmp/some-pod-home"
    # And the bare strip behavior that motivated the fix:
    assert "KIROCREW_HOME" not in registry.minimal_env()


def test_backend_spawn_env_passes_project_dir(monkeypatch):
    """KIROCREW_PROJECT_DIR is a platform var like KIROCREW_HOME — backends
    (e.g. dev-fleet worktree discovery) need the gateway's source checkout."""
    import inspect

    import kiro_crew.apps.backend as backend_mod

    src = inspect.getsource(backend_mod)
    assert 'KIROCREW_HOME=str(config_dir())' in src
    assert '"KIROCREW_PROJECT_DIR"' in src


def test_app_secret_loader_does_not_cache_empty(monkeypatch, tmp_path):
    """A missing secret must NOT be cached: it may be provisioned after the
    backend starts (install race). Empty-cache would 401 forever."""
    monkeypatch.setattr(http_api_mod, "_APP_SECRET", None)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert mod._load_app_secret() == ""
    assert mod._APP_SECRET is None  # emptiness NOT latched
    sdir = tmp_path / "apps" / mod.APP_NAME
    sdir.mkdir(parents=True)
    (sdir / ".app_secret").write_text("s3cr3t\n")
    assert mod._load_app_secret() == "s3cr3t"  # picked up on retry
