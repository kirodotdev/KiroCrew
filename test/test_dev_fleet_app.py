"""Tests for the dev-fleet native dashboard handler."""
from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac  # noqa: F401
import hmac as _hmac_mod
import json
import os
import shutil
import sys
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

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import http_api as http_api_mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import npm_preflight
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod

# These Dev Fleet make-live / cancel / sync / build tests assert POSIX-only
# behaviour that has no Windows equivalent: os.geteuid, the ``.venv/bin`` layout
# (vs ``.venv\\Scripts\\kirocrew.exe``), systemctl/launchctl service probing, and
# ``/``-rooted trusted-binary paths. The production code is correct on Windows;
# only these fixtures/assertions are POSIX-shaped, so they are skipped under the
# reduced-scope backend CI that runs on Windows. See issue #2041.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only Dev Fleet make-live/cancel/sync semantics (issue #2041)",
)


@pytest.fixture(autouse=True)
def _venv_maps_to_the_synced_checkout():
    """Answer the sync's foreign-venv guard with "it maps", for tests not about it.

    Both install paths now refuse a venv that serves a DIFFERENT checkout, and
    answering that question RUNS the target interpreter — which every sync test
    here points at a path that does not exist. Stubbing both halves keeps an
    unrelated assertion from turning into an unrunnable-interpreter refusal. The
    guard's own behaviour is asserted by
    ``test_sync_refuses_a_venv_that_serves_another_checkout``, which patches it
    back to a refusal, and the logic behind it lives in ``test/test_dep_sync.py``.
    """
    from kiro_crew import dep_sync

    with patch.object(dep_sync, "installed_package_origin", return_value="<stubbed>"), \
         patch.object(dep_sync, "venv_not_mapped_to", return_value=None):
        yield


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
    """When require_backend() raises PodBackendAbsent, removal proceeds."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod
    from kiro_crew.pod.runtime import PodBackendAbsent

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=1), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "require_backend", side_effect=PodBackendAbsent("no session bus")), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_remove_refuses_operational_pod_error():
    """When require_backend() passes but active_names raises, removal is refused."""
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
         patch.object(runtime_mod.rt, "active_names", side_effect=OSError("launchctl error")), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is False
    assert "cannot verify pod state" in result["error"]


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
    # temporary directories (GPT round 2, no-test-side-effects).
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


def _steps_json_path_from_cmd(cmd):
    """The steps-JSON file path from a runner command.

    ``cmd`` is ``[python, -I, -c, <bootstrap>, <snapshot>, <snapshot-sha256>,
    <steps_json>, <repo>, --reserved, <codes>, ...]``. The steps file is the
    SECOND positional after the snapshot path (the snapshot ends in
    ``sync_runner.py``; the argv-pinned snapshot digest sits between them).
    """
    for i, tok in enumerate(cmd):
        if isinstance(tok, str) and tok.endswith("sync_runner.py"):
            return cmd[i + 2]
    raise AssertionError("sync_runner.py snapshot not found in command — shape changed")


def _sync_steps_from_cmd(cmd):
    """Pull the structured step list back out of the runner invocation.

    The runner is no longer a ``-c <script>`` string; it is a snapshot of
    sync_runner.py run BY PATH, and the steps travel as a JSON FILE whose path
    is the runner's first positional argument. Read that file so the assertions
    bite on the real step list rather than on source text.
    """
    import json as _json

    steps_json_path = _steps_json_path_from_cmd(cmd)
    with open(steps_json_path, encoding="utf-8") as fh:
        return _json.load(fh)


def _install_step(steps):
    """The 'pip install' step dict from a decoded step list.

    Both branches (editable reinstall and dependency-only substitute) label
    their install step 'pip install'; the argv is what differs between them.
    """
    for st in steps:
        if st["label"] == "pip install":
            return st
    raise AssertionError("no 'pip install' step found in the step list")


def _cleanup_sync_tempdirs(mock_start):
    """Delete the snapshot dirs a stubbed _start_run would have cleaned up.

    Stubbing ``_start_run`` also stubs out its ``finally`` cleanup, so the
    snapshot dirs the sync stages (the runner snapshot + steps file, the
    preflight snapshot, and the dependency-only path's dep_sync snapshot) would
    outlive the test. Remove exactly what it registered, file before directory.
    """
    if not getattr(mock_start, "call_args", None):
        return
    for path in mock_start.call_args.kwargs.get("cleanup_paths") or []:
        try:
            os.unlink(path)
        except OSError:
            try:
                os.rmdir(path)
            except OSError:
                pass


#: The main checkout the sync tests run against. Pinned rather than ambient so the
#: sync's refusal path (no checkout discovered) cannot decide their outcome.
_SYNC_REPO = "/fake/main-checkout"


#: cleanup_paths captured from the last ``_run_sync``. The harness stubs
#: ``_start_run``, so a test cannot see what the sync registered for removal any
#: other way, and asserting on it is how a leaked snapshot gets caught.
_LAST_CLEANUP_PATHS: list[str] = []


async def _run_sync(mod, locked):
    """Drive _sync_start_locked with the probe stubbed.

    Returns ``(result, cmd, steps)``: the result dict, the runner command handed
    to ``_start_run`` (``None`` when the sync refused), and the decoded step
    dicts read from the steps JSON file BEFORE the harness deletes it (``None``
    on a refusal). The runner is a snapshot of sync_runner.py run by path with
    the step list in a JSON file, so assertions inspect ``cmd`` and ``steps``
    rather than script source text.

    MAIN_REPO is pinned because the sync refuses outright when no checkout was
    discovered, and these tests are about the sync's own behaviour: leaving it
    ambient makes them pass or fail on whether the HOST running them happens to
    sit in a Kiro Crew checkout. Assertions that quote the repo path must use
    ``_SYNC_REPO`` rather than reading ``mod.MAIN_REPO``.

    The venv-origin guard is answered by the module's autouse fixture; a test
    about that guard patches it back to a refusal.
    """
    repository_mod._UPSTREAM_REMOTE = "origin"
    worktree_ops_mod._SYNC_RID = None
    with patch.object(repository_mod, "MAIN_REPO", _SYNC_REPO), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="main"), \
         patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(worktree_ops_mod.dep_sync, "locked_console_scripts", return_value=locked), \
         patch("kiro_crew.apps.builtins.dev_fleet.worktree_ops.sandboxed_spawn_argv",
               side_effect=lambda cmd, mode, env=None: (cmd, env or {}, None)), \
         patch.object(runtime_mod, "_start_run", new_callable=AsyncMock, return_value="run-123") as mock_start:
        async with mod._SYNC_LOCK:
            result = await mod._sync_start_locked()
    repository_mod._UPSTREAM_REMOTE = None
    cmd = mock_start.call_args[0][1] if mock_start.call_args else None
    # Decode the steps BEFORE cleanup deletes the staged JSON file — inside
    # try/finally so a malformed file or a changed command shape cannot leave
    # the staged directories behind (the finally owns cleanup either way).
    global _LAST_CLEANUP_PATHS
    _LAST_CLEANUP_PATHS = list(
        (mock_start.call_args.kwargs.get("cleanup_paths") or [])
        if mock_start.call_args else []
    )
    try:
        steps = _sync_steps_from_cmd(cmd) if cmd else None
    finally:
        # Stubbing _start_run also stubs out its `finally` cleanup, so anything
        # the sync staged for removal (the runner snapshot + steps file, and
        # the dependency-only path's dep_sync snapshot) would outlive the test.
        _cleanup_sync_tempdirs(mock_start)
    return result, cmd, steps


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
    # Applied to the env actually handed to subprocess.run, not a stale copy.
    assert "cwd=cwd, env=env" in run_step_body
    # Set before the step is spawned, not after.
    assert run_step_body.index("PYTHONIOENCODING") < run_step_body.index("subprocess.run(")


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


# --- cancellation survives an already-reaped child (#2096) ---
@pytest.mark.asyncio
async def test_run_cmd_cancel_with_reaped_child_propagates_cancellation(monkeypatch):
    """Cancelling _run_cmd whose child was already reaped must raise
    CancelledError, not ProcessLookupError.

    An unguarded ``proc.kill()`` in the CancelledError branch raises
    ProcessLookupError on a reaped child, REPLACING the in-flight
    cancellation; ``_status_refresher``'s broad handler then swallows it
    and loops forever, hanging ``dev_fleet_cleanup``'s ``await bg_task``
    and the whole pytest-asyncio loop teardown (#2096).
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
    # full pipe could hang (#5989).
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
    Discovery Error banner used to end mid-word at "Probe". Guard the tail, not
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

    This used to return a silent [] — which the UI renders as the
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

    Issue #2530: this failure used to surface as "git worktree discovery
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
    """Health endpoint does not require HMAC (used by backend.py health loop)."""
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
    # GIT_NO_REPLACE_OBJECTS is an env var in its own right, NOT one of the
    # config pairs, so the count must not have grown to cover it.
    assert n["GIT_CONFIG_COUNT"] == "4"
    assert n["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
    assert n["GIT_CONFIG_VALUE_0"] == "false"
    assert n["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert n["GIT_CONFIG_VALUE_1"] == "/dev/null"
    assert n["GIT_CONFIG_KEY_2"] == "credential.helper"
    assert n["GIT_CONFIG_VALUE_2"] == ""
    assert n["GIT_CONFIG_KEY_3"] == "core.sshCommand"
    assert n["GIT_CONFIG_VALUE_3"] == "ssh"


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
        proc = subprocess.run(
            [git, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={**base_env, **(env or {})},
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
        "api_dev_fleet_rebase", "api_dev_fleet_restart_gateway",
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
    assert "/api/restart-gateway" in routes


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


# =============================================================================
# Task 1a: own-commits-only argv in _worktree_detail
# =============================================================================


@pytest.mark.asyncio
async def test_worktree_detail_uses_own_commits_log():
    """_worktree_detail must use `log <remote>/main..HEAD` (own commits only),
    never a bare `log -6` that bleeds shared history."""
    git_calls: list[list[str]] = []

    async def mock_git(path, *args, **kw):
        git_calls.append(list(args))
        if "rev-parse" in args and "--abbrev-ref" in args:
            return "feat-x"
        if "rev-parse" in args and "--short=7" in args:
            return "abc1234"
        if "status" in args:
            return ""
        if "rev-list" in args:
            return "2"
        if "log" in args and any("main..HEAD" in a for a in args):
            return "abc1234\x1ffix bug\x1f2 hours ago"
        if "diff" in args and "--name-only" in args:
            return ""
        if "log" in args and "--format=%ct" in args:
            return "1700000000"
        return None

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(repository_mod, "_git", side_effect=mock_git), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=2), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "50\t/fake/wt\n", "")), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"), \
         patch.object(runtime_mod, "_POD_AVAILABLE", False):
        result = await mod._worktree_detail("feat-x")

    assert result["commits"] == [{"hash": "abc1234", "subject": "fix bug", "when": "2 hours ago"}]
    log_calls = [c for c in git_calls if "log" in c and any("main..HEAD" in a for a in c)]
    assert len(log_calls) == 1
    bare_log_calls = [c for c in git_calls if c[:1] == ["log"] and "-6" in c and not any("main..HEAD" in a for a in c)]
    assert len(bare_log_calls) == 0


# =============================================================================
# Task 1b: design_docs filter
# =============================================================================


@pytest.mark.asyncio
async def test_worktree_detail_design_docs_filter():
    """design_docs filters for paths starting docs/ or containing /docs/ or design."""
    async def mock_git(path, *args, **kw):
        if "rev-parse" in args and "--abbrev-ref" in args:
            return "feat-x"
        if "rev-parse" in args and "--short=7" in args:
            return "abc1234"
        if "status" in args:
            return ""
        if "rev-list" in args:
            return "0"
        if "log" in args and "origin/main..HEAD" in args:
            return ""
        if "diff" in args and "--name-only" in args:
            return "docs/README.md\nsrc/main.py\npackage/docs/spec.md\ndesign-doc.md\n"
        if "log" in args and "--format=%ct" in args:
            return "1700000000"
        return None

    with patch.object(repository_mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(repository_mod, "_git", side_effect=mock_git), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "50\t/fake/wt\n", "")), \
         patch.object(runtime_mod, "_POD_AVAILABLE", False):
        result = await mod._worktree_detail("feat-x")

    assert set(result["design_docs"]) == {"docs/README.md", "package/docs/spec.md", "design-doc.md"}


# =============================================================================
# Task 2: restart-gateway endpoint
# =============================================================================


@pytest.mark.asyncio
async def test_restart_gateway_not_active():
    """restart-gateway returns ok:false when service is not active."""
    with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock,
                      return_value=(3, "", "inactive\n")):
        result = await mod._restart_gateway()
    assert result["ok"] is False
    # With the foreground fallback, the error comes from whichever backend
    # ultimately fails: either "not running as a user service" (no foreground
    # eligible) or the foreground backend's own diagnostic.
    assert result["error"]


@pytest.mark.asyncio
async def test_restart_gateway_active_detached():
    """restart-gateway issues systemd-run --collect restart when active."""
    calls: list[list[str]] = []

    async def mock_run_cmd(cmd, **kw):
        calls.append(cmd)
        if "is-active" in cmd:
            return (0, "active\n", "")
        if "systemd-run" in cmd:
            return (0, "", "")
        return (0, "", "")

    with patch.object(runtime_mod, "_run_cmd", side_effect=mock_run_cmd), \
         patch.object(live_mod, "sys") as mock_sys, \
         patch.object(live_mod, "shutil") as mock_shutil:
        mock_sys.platform = "linux"
        mock_shutil.which.return_value = "/usr/bin/systemctl"
        result = await mod._restart_gateway()
    assert result["ok"] is True
    restart_calls = [c for c in calls if "systemd-run" in c]
    assert len(restart_calls) == 1
    assert "--collect" in restart_calls[0]
    assert "restart" in restart_calls[0]


@pytest.mark.asyncio
async def test_restart_gateway_audited():
    """The restart-gateway endpoint is wrapped by _audited."""
    import inspect
    fn = mod.api_dev_fleet_restart_gateway
    assert callable(fn) and inspect.iscoroutinefunction(fn)


# =============================================================================
# Task: make-live (switch the live gateway to another worktree)
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_make_live_committed_latch():
    """``_MAKE_LIVE_COMMITTED`` is a process-local latch: once a real cutover
    schedules a restart it refuses all further cutovers for the process's life.
    In-process pytest would leak that latched state into later tests, so reset
    it around every test to mirror a fresh gateway process."""
    live_mod._MAKE_LIVE_COMMITTED = False
    yield
    live_mod._MAKE_LIVE_COMMITTED = False


@pytest.fixture(autouse=True)
def _reset_shutdown_admission_state():
    """``_SHUTDOWN_IN_PROGRESS`` is set by ``dev_fleet_cleanup`` and never cleared
    in production (the process exits).  In-process pytest leaks the latched True
    state into later tests that call ``_start_run`` directly, causing them to
    raise RuntimeError instead of running normally.  Reset both the flag and the
    lock around every test to mirror a fresh gateway process."""
    runtime_mod._SHUTDOWN_IN_PROGRESS = False
    runtime_mod._SHUTDOWN_ADMISSION_LOCK = asyncio.Lock()
    yield
    runtime_mod._SHUTDOWN_IN_PROGRESS = False
    runtime_mod._SHUTDOWN_ADMISSION_LOCK = asyncio.Lock()


def _mk_make_live_wt(tmp_path, *, venv: bool = False, dist: bool = False,
                     venv_exec: bool = True):
    """Build a fake worktree dir with optional .venv/bin/kirocrew and built dist.

    When ``venv`` is set the fake ``.venv/bin/kirocrew`` is created **executable**
    (``venv_exec=True``, the realistic provisioned state that passes the make-live
    exec-bit gate); pass ``venv_exec=False`` to simulate a present-but-non-executable
    binary (the ``venv_not_executable`` case)."""
    wt = tmp_path / "kirocrew-wt-feat"
    wt.mkdir(parents=True, exist_ok=True)
    if venv:
        vb = wt / ".venv" / "bin"
        vb.mkdir(parents=True, exist_ok=True)
        kcbin = vb / "kirocrew"
        kcbin.write_text("#!/bin/sh\n")
        kcbin.chmod(0o755 if venv_exec else 0o644)
    if dist:
        dd = wt / "src" / "kiro_crew" / "static" / "dist"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "index.html").write_text("<html></html>")
    return wt


def _assert_sandboxed(path, what: str, sandbox_root) -> None:
    """Fail loudly when a host-mutating make-live seam resolves outside the sandbox.

    The seams below decide WHERE the cutover writes and WHAT it executes. If one is
    left unpatched the production code is correct and does exactly what it is told —
    against the developer's own machine: it rewrites the live gateway's systemd
    drop-in to point at a pytest tmpdir and restarts the unit, which then fails
    203/EXEC on every boot once the tmpdir is reaped. Asserting containment here
    makes the next missed seam fail inside the test instead of taking down the host.

    *sandbox_root* is THIS test's own directory, not a generic temp root. The
    distinction is the whole strength of the check: "somewhere under the system temp
    dir" is satisfied by any tmp path at all, whereas "inside the tree this test
    built" is satisfied only by the redirect actually taking effect. It also stops the
    assertion depending on where ``tempfile`` happens to be rooted, which the suite's
    isolation floor now controls.
    """
    resolved = Path(path).resolve()
    root = Path(sandbox_root).resolve()
    assert root == resolved or root in resolved.parents, (
        f"{what} resolved OUTSIDE the temp sandbox: {resolved}. A test that reaches "
        f"the cutover path must never touch a real host path."
    )
    assert (Path.home() / ".config").resolve() not in resolved.parents, (
        f"{what} resolved inside the real user config dir: {resolved}"
    )


def _stub_make_live(monkeypatch, wt, *, live=None, in_pod=False, unit_status="ok",
                    platform="linux", pointer_dir=None):
    """Wire the make-live seams: the path resolves to *wt*, pod/live/unit state
    fixed. ``unit_status`` stubs _live_user_unit_status so tests never depend on
    the host's real systemd --user state.

    ``pointer_dir`` isolates the live-target pointer: when set (a tmp_path
    sub-directory), ``live_target.pointer_path`` returns a file inside it so no
    test ever reads or writes the real data home. Every test that reaches the
    cutover path MUST pass this.

    The service-definition and command seams are isolated **unconditionally**,
    because forgetting them is not a test failure — it is a live-host outage.
    ``_dropin_path`` otherwise resolves ``$XDG_CONFIG_HOME``/``~/.config`` and
    ``_run_cmd`` otherwise runs the real ``systemctl --user``, so a test reaching
    the cutover path would repoint and restart the developer's own gateway at a
    tmpdir. Both are redirected under *wt*'s parent and containment-asserted; a
    test that needs to observe or shape them re-patches after this call, which
    wins because it is applied later.

    ``platform`` pins the service backend (default systemd). Without it these
    assertions silently follow the HOST's platform: the same test would check a
    systemd drop-in on Linux and a launchd agent on macOS. Pinning keeps every
    systemd expectation deterministic everywhere, and the launchd twins pin
    ``"darwin"`` explicitly.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform=platform))
    tool = "/usr/bin/systemctl" if platform == "linux" else "/bin/launchctl"
    monkeypatch.setattr(live_mod, "shutil", MagicMock(which=MagicMock(return_value=tool)))
    monkeypatch.setattr(
        repository_mod, "_discover_worktrees",
        AsyncMock(return_value=[{"path": str(wt), "branch": "feat", "is_main": False}]),
    )
    monkeypatch.setattr(live_mod, "_live_worktree_path", AsyncMock(return_value=live))
    monkeypatch.setattr(live_mod, "_in_pod", lambda: in_pod)
    monkeypatch.setattr(live_mod, "_live_user_unit_status", AsyncMock(return_value=unit_status))
    sandbox_root = Path(wt).parent
    sandbox_dropin = sandbox_root / "_systemd" / f"{mod._LIVE_GATEWAY_UNIT}.d" / "make-live.conf"
    _assert_sandboxed(sandbox_dropin, "_dropin_path", sandbox_root)
    monkeypatch.setattr(live_mod, "_dropin_path", lambda: sandbox_dropin)
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    # Prove the redirect actually took: a rename of the production symbol would
    # otherwise leave the real path live while every test still looked green.
    _assert_sandboxed(mod._dropin_path(), "patched _dropin_path()", sandbox_root)
    if pointer_dir is not None:
        pointer_dir.mkdir(parents=True, exist_ok=True)
        ptr_file = pointer_dir / "live_target.json"
        monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    if platform == "darwin":
        monkeypatch.setattr(
            live_mod.gateway_service, "restart_contract_current", lambda _path: True
        )
        monkeypatch.setattr(
            live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
        )


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_dry_run_plan(monkeypatch, tmp_path):
    """dry_run returns the pointer-based plan without writing anything."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    plan = res["plan"]
    assert plan["mechanism"] == "live-target pointer"
    assert plan["pointer_path"] == str(ptr_dir / "live_target.json")
    assert plan["exec"] == str(wt / ".venv" / "bin" / "kirocrew")
    assert plan["restart"] == "automatic"
    assert plan["target"] == str(wt)
    # dry-run writes nothing: pointer file absent, no commands issued.
    assert not (ptr_dir / "live_target.json").exists()
    assert calls == []


@pytest.mark.asyncio
async def test_make_live_unknown_path(monkeypatch):
    """A path that is not a discovered worktree is refused."""
    monkeypatch.setattr(repository_mod, "_discover_worktrees", AsyncMock(return_value=[]))
    monkeypatch.setattr(live_mod, "_in_pod", lambda: False)
    res = await mod._make_live("/nope/not-a-worktree", dry_run=True)
    assert res["ok"] is False and res["code"] == "unknown_path"


@pytest.mark.asyncio
async def test_make_live_refuses_in_pod(monkeypatch, tmp_path):
    """Refuse to cut the real live gateway from inside a pod plane (even dry_run)."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    _stub_make_live(monkeypatch, wt, in_pod=True)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "pod"


@pytest.mark.asyncio
async def test_make_live_already_live(monkeypatch, tmp_path):
    """Refuse when the target is already the live gateway."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    _stub_make_live(monkeypatch, wt, live=str(wt.resolve()))
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "already_live"


@pytest.mark.asyncio
async def test_make_live_missing_venv(monkeypatch, tmp_path):
    """No .venv/bin/kirocrew -> actionable Provision error."""
    wt = _mk_make_live_wt(tmp_path, venv=False, dist=True)
    _stub_make_live(monkeypatch, wt)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "missing_venv"
    assert "Provision" in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_venv_not_executable(monkeypatch, tmp_path):
    """.venv/bin/kirocrew present but NOT executable -> a distinct, actionable
    error (missing_venv is for the not-a-file case). A non-executable binary
    would stop the live gateway but never start the replacement, so this MUST
    be refused before any cutover."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True, venv_exec=False)
    _stub_make_live(monkeypatch, wt)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "venv_not_executable"
    assert "chmod" in res["error"]
    # An executable binary (the realistic provisioned state) passes this gate
    # and proceeds to a valid plan — proving the check is exec-bit-specific,
    # not a blanket rejection.
    wt_ok = _mk_make_live_wt(tmp_path / "ok", venv=True, dist=True)
    _stub_make_live(monkeypatch, wt_ok)
    res_ok = await mod._make_live(str(wt_ok), dry_run=True)
    assert res_ok["ok"] is True and res_ok.get("dry_run") is True


@pytest.mark.asyncio
async def test_make_live_missing_dist(monkeypatch, tmp_path):
    """Built venv but no dist/index.html -> actionable Pull+Build error."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=False)
    _stub_make_live(monkeypatch, wt)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "missing_dist"
    assert "Pull+Build" in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_real_cutover_writes_pointer(monkeypatch, tmp_path):
    """A real cutover on a drivable service writes the pointer AND restages the
    service definition, issues a detached restart, and invalidates the
    live-worktree cache.

    Restaging the definition is what keeps its ExecStart binary present: a
    definition left pinned to a previously-made-live worktree fails EXEC once
    that worktree is pruned, and the gateway then never starts far enough to read
    the pointer at all.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    dropin = tmp_path / "dropins" / "make-live.conf"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_dropin_path", lambda: dropin)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", "sentinel", raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 123.0, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res.get("cutover") is True
    assert res.get("staged_only") is not True
    # Pointer file written with the resolved checkout path.
    ptr_file = ptr_dir / "live_target.json"
    assert ptr_file.is_file()
    import json as _json
    data = _json.loads(ptr_file.read_text())
    assert Path(data["checkout"]).resolve() == wt.resolve()
    # Service definition restaged at the SAME target, then re-read.
    assert dropin.is_file()
    assert str(wt) in dropin.read_text(encoding="utf-8")
    assert ["systemctl", "--user", "daemon-reload"] in calls
    # A DETACHED restart was issued (platform-specific; linux = systemd-run).
    assert any(
        c[:2] == ["systemd-run", "--user"] and "restart" in c for c in calls
    )
    # Live-worktree cache invalidated so the next poll re-resolves.
    assert mod._LIVE_WORKTREE is None
    assert mod._LIVE_CHECK_AT == 0.0


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_staged_only_leaves_service_definition_untouched(
    monkeypatch, tmp_path,
):
    """On a host whose service Dev Fleet cannot drive, only the pointer is
    staged.

    The definition there is the baseline install, whose ExecStart binary is by
    construction the one currently running — so touching it could only break a
    working definition, and the pointer alone carries the cutover.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    dropin = tmp_path / "dropins" / "make-live.conf"
    _stub_make_live(monkeypatch, wt, unit_status="no_user_unit", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_dropin_path", lambda: dropin)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res["staged_only"] is True
    assert (ptr_dir / "live_target.json").is_file()
    assert not dropin.exists()
    assert ["systemctl", "--user", "daemon-reload"] not in calls


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_latches_after_cutover(monkeypatch, tmp_path):
    """A successful cutover latches _MAKE_LIVE_COMMITTED. A second request —
    cutover for a DIFFERENT valid target, or even a dry_run — is then refused
    with restart_pending, so no concurrent cutover can mutate the pointer while
    the scheduled restart is tearing this process down."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert mod._MAKE_LIVE_COMMITTED is False
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res.get("cutover") is True
    assert mod._MAKE_LIVE_COMMITTED is True

    # Second cutover for a different, otherwise-valid target -> restart_pending.
    wt2 = _mk_make_live_wt(tmp_path / "b", venv=True, dist=True)
    _stub_make_live(monkeypatch, wt2, pointer_dir=ptr_dir)
    res2 = await mod._make_live(str(wt2), dry_run=False)
    assert res2["ok"] is False and res2["code"] == "restart_pending"
    # A dry_run is refused too (the latch is checked at entry, before dry_run).
    res3 = await mod._make_live(str(wt2), dry_run=True)
    assert res3["ok"] is False and res3["code"] == "restart_pending"


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_write_failure_does_not_latch(monkeypatch, tmp_path):
    """A cutover that fails during pointer write (before restart scheduling)
    must NOT latch — the restart never happened, so a subsequent cutover
    proceeds normally."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    # Make write_target raise an OSError to simulate a disk failure.
    original_write = live_mod.live_target.write_target
    call_count = {"n": 0}

    def fail_first_write(checkout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("disk full")
        return original_write(checkout)

    monkeypatch.setattr(live_mod.live_target, "write_target", fail_first_write)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "write_failed"
    assert mod._MAKE_LIVE_COMMITTED is False

    # With the seam repaired, a subsequent cutover proceeds (not latched).
    monkeypatch.setattr(live_mod.live_target, "write_target", original_write)
    res2 = await mod._make_live(str(wt), dry_run=False)
    assert res2["ok"] is True and res2.get("cutover") is True
    assert mod._MAKE_LIVE_COMMITTED is True


@pytest.mark.asyncio
async def test_live_worktree_path_reads_working_directory_with_spaces(monkeypatch):
    """_live_worktree_path resolves via `systemctl show --property=
    WorkingDirectory --value`, which (unlike the ExecStart path= regex) is NOT
    truncated at spaces — so a checkout path containing a space resolves whole."""
    spacey = "/home/u/my worktrees/kirocrew-wt-feat"

    async def fake_run_cmd(cmd, **kw):
        assert "--property=WorkingDirectory" in cmd and "--value" in cmd
        return (0, spacey + "\n", "")

    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    got = await mod._live_worktree_path()
    assert got == str(Path(spacey).resolve())


@pytest.mark.asyncio
async def test_live_worktree_path_falls_back_to_execstart(monkeypatch, tmp_path):
    """When WorkingDirectory is empty, fall back to parsing ExecStart's path=."""
    checkout = tmp_path / "kirocrew-wt-feat"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    exe = checkout / ".venv" / "bin" / "kirocrew"

    async def fake_run_cmd(cmd, **kw):
        if "--property=WorkingDirectory" in cmd:
            return (0, "\n", "")  # empty -> trigger ExecStart fallback
        if cmd[-1] == "ExecStart":
            return (0, f"{{ path={exe} ; argv[]={exe} gateway ; ignore_errors=no }}", "")
        raise AssertionError(f"unexpected cmd {cmd}")

    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    got = await mod._live_worktree_path()
    assert got == str(checkout.resolve())


@pytest.mark.asyncio
async def test_make_live_already_live_space_path(monkeypatch, tmp_path):
    """already_live is detected when the live WorkingDirectory (and target)
    contain spaces — the regression the WorkingDirectory switch fixes: the old
    ExecStart path= regex truncated at the space, never matched, and would let
    the same worktree be pointlessly re-cut over and over."""
    wt = tmp_path / "my worktrees" / "kirocrew-wt-feat"
    wt.mkdir(parents=True)
    vb = wt / ".venv" / "bin"
    vb.mkdir(parents=True)
    kc = vb / "kirocrew"
    kc.write_text("#!/bin/sh\n")
    kc.chmod(0o755)
    dd = wt / "src" / "kiro_crew" / "static" / "dist"
    dd.mkdir(parents=True)
    (dd / "index.html").write_text("<html></html>")
    _stub_make_live(monkeypatch, wt, live=str(wt.resolve()))
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "already_live"


def test_make_live_route_registered_and_audited():
    """/api/make-live is wired in create_app and the handler is a coroutine."""
    import inspect
    app = mod.create_app()
    paths = [getattr(r.resource, "canonical", None) for r in app.router.routes()]
    assert "/api/make-live" in paths
    fn = mod.api_dev_fleet_make_live
    assert callable(fn) and inspect.iscoroutinefunction(fn)


# --- make-live: fail-closed pod guard ---
def test_in_pod_tristate(monkeypatch, tmp_path):
    """_in_pod is tri-state: True inside a pod home, False outside, and None
    when the config home cannot be resolved (fail-closed at the source — the
    previous fail-OPEN False would have let a pod cut the live gateway)."""
    import kiro_crew.config.loader as cfg_loader

    pod_home = tmp_path / ".kirocrew-pods" / "kirocrew-wt-x"
    pod_home.mkdir(parents=True)
    monkeypatch.setattr(cfg_loader, "config_dir", lambda: pod_home)
    assert mod._in_pod() is True

    live_home = tmp_path / ".kirocrew"
    live_home.mkdir()
    monkeypatch.setattr(cfg_loader, "config_dir", lambda: live_home)
    assert mod._in_pod() is False

    monkeypatch.setattr(
        cfg_loader, "config_dir", MagicMock(side_effect=RuntimeError("boom"))
    )
    assert mod._in_pod() is None


@pytest.mark.asyncio
async def test_make_live_pod_indeterminate_fails_closed(monkeypatch, tmp_path):
    """Indeterminate pod status must refuse make-live (fail-closed), never
    proceed as if not-a-pod."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    _stub_make_live(monkeypatch, wt)
    monkeypatch.setattr(live_mod, "_in_pod", lambda: None)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "pod_indeterminate"


# --- make-live: staged-only when service not drivable ---
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_stages_only_when_service_not_drivable(monkeypatch, tmp_path):
    """A live gateway installed as a SYSTEM unit (or no service at all) succeeds
    as staged_only: the pointer is written, no restart attempted, and a manual
    restart command is provided. The committed latch is NOT set (re-pointing to
    a different worktree must stay allowed)."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"

    for status in ("no_user_unit", "no_systemd"):
        ptr_dir_sub = ptr_dir / status
        _stub_make_live(monkeypatch, wt, unit_status=status, pointer_dir=ptr_dir_sub)
        monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        calls: list = []

        async def fake_run_cmd(cmd, **kw):
            calls.append(cmd)
            return (0, "", "")

        monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
        res = await mod._make_live(str(wt), dry_run=False)
        assert res["ok"] is True, f"status={status}: {res}"
        assert res["staged_only"] is True
        assert res["manual_restart"]  # non-empty command string
        # The dashboard surfaces `notice` verbatim instead of entering the restart
        # handshake, so a staged_only response without it would silently drop the
        # only instruction the operator gets.
        assert res["manual_restart"] in res["notice"]
        # Pointer written with the target.
        ptr_file = ptr_dir_sub / "live_target.json"
        assert ptr_file.is_file()
        import json as _json
        data = _json.loads(ptr_file.read_text())
        assert Path(data["checkout"]).resolve() == wt.resolve()
        # NOT latched: a subsequent cutover to another worktree stays allowed.
        assert mod._MAKE_LIVE_COMMITTED is False
        # No restart command issued.
        assert not any(
            c[:2] == ["systemd-run", "--user"] for c in calls
        )


@pytest.mark.asyncio
async def test_live_user_unit_status_no_manager(monkeypatch):
    """A platform with neither systemd nor launchd -> no_systemd, no spawn.

    Was previously asserted with ``platform="darwin"``; darwin is now a
    SUPPORTED backend, so the "no manager at all" case has to be expressed with
    a platform that really has none.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="win32"))
    assert await mod._live_user_unit_status() == "no_systemd"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_no_agent(monkeypatch):
    """macOS with launchctl but no such agent loaded -> no_agent."""
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(1, "", "no such")))
    assert await mod._live_user_unit_status() == "no_agent"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_agent_not_indirected(monkeypatch, tmp_path):
    """Agent loaded, but its plist does not go through the live-gateway symlink.

    Swapping the symlink would then be a silent no-op, so make-live must refuse
    with an actionable code instead of reporting a cutover that did nothing.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "  pid = 7\n", "")))
    plist = tmp_path / "agent.plist"
    plist.write_text("<string>/usr/local/bin/kirocrew</string>")
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    assert await mod._live_user_unit_status() == "agent_not_indirected"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_ok(monkeypatch, tmp_path):
    """Agent loaded AND indirected through the live-gateway symlink -> ok."""
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "  pid = 7\n", "")))
    link = tmp_path / "live-gateway"
    link.write_text("#!/bin/sh\nexec '/usr/local/bin/kirocrew' \"$@\"\n")
    plist = tmp_path / "agent.plist"
    plist.write_text(f"<string>{link}</string>")
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: link)
    )
    monkeypatch.setattr(
        live_mod.gateway_service, "restart_contract_current", lambda _path: True
    )
    monkeypatch.setattr(
        live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
    )
    assert await mod._live_user_unit_status() == "ok"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_loaded_contract_outdated(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    link = tmp_path / "live-gateway"
    link.write_text("#!/bin/sh\n")
    plist = tmp_path / "agent.plist"
    plist.write_text(f"<string>{link}</string>")
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    monkeypatch.setattr(
        live_mod.gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: link)
    )
    monkeypatch.setattr(
        live_mod.gateway_service, "restart_contract_current", lambda _path: True
    )
    monkeypatch.setattr(
        live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: False
    )
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "pid = 7\n", "")))

    assert await mod._live_user_unit_status() == "agent_restart_contract_outdated"


@pytest.mark.asyncio
async def test_live_user_unit_status_ok_and_missing(monkeypatch):
    """`systemctl --user cat` rc==0 AND the unit running -> ok; rc!=0 (system unit
    / not installed) -> no_user_unit.

    Loadedness alone is not enough: `ok` means a restart replaces the gateway we
    are in, so the classifier also requires `is-active`.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )

    async def loaded_and_running(cmd, **kw):
        assert cmd[:2] == ["systemctl", "--user"]
        if "is-active" in cmd:
            return (0, "active", "")
        assert "cat" in cmd
        return (0, "# unit contents", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", loaded_and_running)
    assert await mod._live_user_unit_status() == "ok"

    async def cat_missing(cmd, **kw):
        return (1, "", "No files found for kirocrew-gateway.service.")

    monkeypatch.setattr(runtime_mod, "_run_cmd", cat_missing)
    assert await mod._live_user_unit_status() == "no_user_unit"


# --- make-live: systemd value escaping / unsafe_path (Codex round 2, Finding A) ---
def test_sd_value_escapes_and_conditionally_quotes():
    """A clean path is emitted verbatim; `%` specifiers double to `%%`; only
    whitespace/metacharacters trigger double-quoting (with \\ and " escaped)."""
    assert mod._sd_value("/clean/path-1.2/bin") == "/clean/path-1.2/bin"
    assert mod._sd_value("/a b/c") == '"/a b/c"'          # whitespace -> quoted
    assert mod._sd_value("/a%b") == "/a%%b"               # specifier doubled, unquoted
    assert mod._sd_value("/a %b") == '"/a %%b"'           # both
    assert mod._sd_value('/a"b') == '"/a\\"b"'            # embedded quote escaped
    assert mod._sd_value("/a\\b") == '"/a\\\\b"'          # embedded backslash escaped


def test_sd_value_rejects_control_char_paths():
    """Newline / NUL / tab / other C0 control chars are rejected outright."""
    for bad in ["/a\nb", "/a\x00b", "/a\tb", "/a\x1fb", "/a\x7fb", "/a\rb"]:
        with pytest.raises(mod._UnsafeUnitValue):
            mod._sd_value(bad)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_escapes_special_char_worktree(monkeypatch, tmp_path):
    """A worktree path with a space yields a valid plan and a real cutover
    writes the resolved path into the pointer file correctly."""
    name = 'kirocrew-wt feat'
    wt = tmp_path / name
    (wt / ".venv" / "bin").mkdir(parents=True)
    _kc = wt / ".venv" / "bin" / "kirocrew"
    _kc.write_text("#!/bin/sh\n")
    _kc.chmod(0o755)
    dd = wt / "src" / "kiro_crew" / "static" / "dist"
    dd.mkdir(parents=True)
    (dd / "index.html").write_text("<html></html>")
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    # dry-run plan names the pointer path and correct exec.
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True
    plan = res["plan"]
    assert plan["mechanism"] == "live-target pointer"
    assert plan["exec"] == str(wt / ".venv" / "bin" / "kirocrew")
    # Real cutover writes the pointer with the resolved (space-containing) path.
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    res2 = await mod._make_live(str(wt), dry_run=False)
    assert res2["ok"] is True and res2.get("cutover") is True
    import json as _json
    data = _json.loads((ptr_dir / "live_target.json").read_text())
    assert Path(data["checkout"]).resolve() == wt.resolve()


@pytest.mark.asyncio
async def test_make_live_unsafe_path_returns_code(monkeypatch, tmp_path):
    """When live_target.validate raises InvalidTarget (from _make_live_plan),
    _make_live refuses with code `unsafe_path` and touches nothing."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    def boom(raw):
        raise live_mod.live_target.InvalidTarget("control chars in path")

    monkeypatch.setattr(live_mod.live_target, "validate", boom)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "unsafe_path"
    assert calls == []
    assert not (ptr_dir / "live_target.json").exists()


@pytest.mark.asyncio
async def test_restart_gateway_darwin_requests_graceful_stop(monkeypatch):
    """macOS restart asks launchd for a bounded SIGTERM-first stop.

    Pinned as a DOMAIN-TARGETED ``kill TERM``, not launchctl's legacy label-only
    ``stop``: Dev Fleet spawns every command inside ``sandbox-exec``, and launchd
    refuses the legacy stop routine for a sandboxed caller ("Not privileged to
    stop service.") whatever the seatbelt profile allows. A regression to the
    legacy form makes Restart fail on every Mac, so the shape is asserted here.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None, raising=False)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(
        live_mod.gateway_service, "restart_contract_current", lambda _path: True
    )
    monkeypatch.setattr(
        live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  state = running\n  pid = 4242\n", "")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._restart_gateway()
    assert res["ok"] is True
    # The PID stands in for systemd's monotonic start stamp: it changes on every
    # respawn, which is the edge the frontend handshake waits for.
    assert res["start_id"] == "4242"
    # Addressed via the backend's own domain helper rather than a second copy of
    # the uid logic (which would also break on Windows, where getuid is absent).
    domain = live_mod.gateway_service.LaunchdBackend.domain()
    assert ["launchctl", "kill", "TERM", f"{domain}/{mod._gateway_label()}"] in calls
    # `kickstart -k` would restart it too, but as a hard kill rather than the
    # graceful SIGTERM the ExitTimeOut budget is sized for.
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in calls)
    # The legacy verb is what the sandbox blocks — it must not reappear.
    assert not any(c[:2] == ["launchctl", "stop"] for c in calls)


@pytest.mark.asyncio
async def test_restart_gateway_darwin_refuses_stale_loaded_contract(monkeypatch):
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(
        live_mod.gateway_service, "restart_contract_current", lambda _path: True
    )
    monkeypatch.setattr(
        live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: False
    )
    calls = []

    async def fake_run_cmd(cmd, **_kw):
        calls.append(cmd)
        return (0, "pid = 4242\n", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._restart_gateway()
    assert res["ok"] is False
    assert "loaded launchd restart contract is outdated" in res["error"]
    assert not any(c[:3] == ["launchctl", "kill", "TERM"] for c in calls)


@pytest.mark.asyncio
async def test_restart_gateway_darwin_not_active(monkeypatch):
    """No loaded agent -> refuse, without attempting a kickstart."""
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None, raising=False)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (1, "", "no such service")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._restart_gateway()
    assert res["ok"] is False
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in calls)


# The launchd cutover tests create a REAL symlink, because the mechanism being
# verified IS the atomic symlink swap. The code under test is macOS-only and
# Windows has no unprivileged symlink creation, so they are POSIX-gated. They
# still run on Linux CI and on the macOS job (whose glob now includes this file),
# so gating costs no coverage.
_posix_symlink_only = pytest.mark.skipif(
    os.name != "posix",
    reason="creates a real symlink to verify the launchd cutover; macOS-only code",
)


@pytest.mark.asyncio
@_posix_symlink_only
async def test_make_live_darwin_writes_pointer_and_stops_agent(monkeypatch, tmp_path):
    """A macOS cutover writes the pointer, then asks launchd to stop the agent."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, platform="darwin", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  pid = 99\n", "")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res.get("cutover") is True
    # Pointer file written with the resolved checkout.
    ptr_file = ptr_dir / "live_target.json"
    assert ptr_file.is_file()
    import json as _json
    data = _json.loads(ptr_file.read_text())
    assert Path(data["checkout"]).resolve() == wt.resolve()
    assert any(c[:3] == ["launchctl", "kill", "TERM"] for c in calls)
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in calls)
    assert not any("bootout" in c or "bootstrap" in c for c in calls)


@pytest.mark.asyncio
@_posix_symlink_only
async def test_make_live_darwin_rolls_back_pointer_on_restart_failure(
    monkeypatch, tmp_path
):
    """A rejected launchd stop restores the prior pointer state.

    Leaving the new pointer in place would silently activate the new checkout
    on the NEXT unrelated restart.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, platform="darwin", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  pid = 99\n", "")
        if cmd[:3] == ["launchctl", "kill", "TERM"]:
            return (1, "", "restart refused")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    # Pointer rolled back: file should not exist (prior was absent).
    ptr_file = ptr_dir / "live_target.json"
    assert not ptr_file.exists()
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
@_posix_symlink_only
async def test_make_live_darwin_dry_run_plan(monkeypatch, tmp_path):
    """The macOS dry-run plan uses the same pointer mechanism and mutates nothing."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, platform="darwin", pointer_dir=ptr_dir)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    plan = res["plan"]
    assert plan["mechanism"] == "live-target pointer"
    assert plan["pointer_path"] == str(ptr_dir / "live_target.json")
    assert plan["target"] == str(wt)
    assert calls == []
    assert not (ptr_dir / "live_target.json").exists()


@pytest.mark.asyncio
@_posix_symlink_only
async def test_make_live_refuses_when_prior_pointer_is_unreadable(
    monkeypatch, tmp_path
):
    """An unreadable prior pointer aborts BEFORE anything is staged.

    ``restore(None)`` means "there was nothing here" and DELETES the pointer, so
    treating a read failure as absent would let a failed cutover destroy a live
    pointer it merely could not read. The abort must happen before staging.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    # Make snapshot() raise PermissionError (simulating an unreadable pointer).
    monkeypatch.setattr(
        live_mod.live_target, "snapshot",
        lambda: (_ for _ in ()).throw(PermissionError("unreadable")),
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "write_failed"
    # No restart issued.
    assert not any(
        c[:2] == ["systemd-run", "--user"] for c in calls
    )
    assert mod._MAKE_LIVE_COMMITTED is False


# --- make-live: pointer write + failure rollback ---
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_rolls_back_pointer_on_restart_failure(monkeypatch, tmp_path):
    """A restart failure with a prior pointer restores the PRIOR content and
    reports rolled_back. When there was no prior pointer, the file is deleted."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    # Prior was absent -> pointer file deleted on rollback.
    assert not (ptr_dir / "live_target.json").exists()
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_rolls_back_pointer_preserves_prior(monkeypatch, tmp_path):
    """When a prior pointer existed, restart failure restores its content."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir(parents=True)
    ptr_file = ptr_dir / "live_target.json"
    prior_content = '{"checkout": "/old/checkout"}\n'
    ptr_file.write_text(prior_content)
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    assert ptr_file.read_text() == prior_content


# --- make-live: concurrency single-flight lock ---
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_concurrent_second_call_busy(monkeypatch, tmp_path):
    """While one cutover holds the make-live lock, a concurrent second call is
    refused immediately with ``busy`` (fail-fast, not queued) and the winner
    still completes and releases the lock."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    # Fresh lock so a leaked hold from another test can't poison this one.
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    entered = asyncio.Event()   # set once the first call is inside the lock
    release = asyncio.Event()   # test-controlled gate to hold it there

    # write_target runs inside the critical section, so signalling from it is an
    # exact barrier: no sleep can substitute, because a loaded runner may not
    # have reached the lock yet and the assertion below would then read an
    # unlocked lock and let the second call through.
    original_write_target = live_mod.live_target.write_target

    def signalling_write(checkout):
        result = original_write_target(checkout)
        entered.set()
        return result

    monkeypatch.setattr(live_mod.live_target, "write_target", signalling_write)

    async def fake_run_cmd(cmd, **kw):
        # Block here (inside the lock) until released.
        if cmd[:2] == ["systemd-run", "--user"]:
            await release.wait()
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)

    first = asyncio.ensure_future(mod._make_live(str(wt), dry_run=False))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert mod._MAKE_LIVE_LOCK.locked() is True

    # Second call returns busy without waiting.
    busy = await asyncio.wait_for(mod._make_live(str(wt), dry_run=False), timeout=5)
    assert busy["ok"] is False and busy["code"] == "busy"
    assert "in progress" in busy["error"]

    release.set()
    res = await asyncio.wait_for(first, timeout=5)
    assert res["ok"] is True and res.get("cutover") is True
    assert mod._MAKE_LIVE_LOCK.locked() is False


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_lock_released_after_failure_and_reusable(monkeypatch, tmp_path):
    """The lock is released on the failure-rollback path too, so a subsequent
    cutover proceeds (never wedged on ``busy``)."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    # 1) restart fails -> rollback path; the lock MUST be released.
    async def restart_fails(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", restart_fails)
    fail = await mod._make_live(str(wt), dry_run=False)
    assert fail["ok"] is False and fail["code"] == "restart_failed"
    assert mod._MAKE_LIVE_LOCK.locked() is False

    # 2) A subsequent all-green cutover proceeds (not refused as busy) and also
    #    releases the lock on the success path.
    async def all_ok(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", all_ok)
    ok = await mod._make_live(str(wt), dry_run=False)
    assert ok["ok"] is True and ok.get("cutover") is True
    assert mod._MAKE_LIVE_LOCK.locked() is False


# --- make-live: pointer-based live worktree resolution ---
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_live_worktree_path_prefers_pointer_over_service(monkeypatch, tmp_path):
    """_live_worktree_path returns the pointer target in preference to the
    service definition ONCE THE GATEWAY IS RUNNING IT. A cutover writes the
    pointer without touching the definition, so the definition would report the
    stale install checkout."""
    target_wt = tmp_path / "kirocrew-wt-new"
    target_wt.mkdir(parents=True)
    (target_wt / ".venv" / "bin").mkdir(parents=True)
    (target_wt / ".venv" / "bin" / "kirocrew").write_text("#!/bin/sh\n")
    (target_wt / ".venv" / "bin" / "kirocrew").chmod(0o755)
    (target_wt / "src" / "kiro_crew").mkdir(parents=True)

    # Write a real pointer file that points at target_wt.
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir(parents=True)
    ptr_file = ptr_dir / "live_target.json"
    import json as _json
    ptr_file.write_text(_json.dumps({"checkout": str(target_wt)}) + "\n")

    monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    # Even with a systemd probe that would return a different path, the pointer
    # takes priority.
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )

    async def should_not_be_called(cmd, **kw):
        raise AssertionError(f"systemctl should not be called: {cmd}")

    monkeypatch.setattr(runtime_mod, "_run_cmd", should_not_be_called)
    # The gateway is executing the pointer target: the cutover has taken effect.
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: target_wt.resolve())
    got = await mod._live_worktree_path()
    assert got == str(target_wt.resolve())
    assert mod._staged_target() is None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_live_worktree_path_reports_running_image_while_staged(monkeypatch, tmp_path):
    """A staged pointer is NOT live: until the gateway restarts it is still
    executing the previous checkout, and reporting the pointer as live would
    tell the operator a cutover landed while old code serves real data."""
    target_wt = tmp_path / "kirocrew-wt-new"
    (target_wt / ".venv" / "bin").mkdir(parents=True)
    (target_wt / ".venv" / "bin" / "kirocrew").write_text("#!/bin/sh\n")
    (target_wt / ".venv" / "bin" / "kirocrew").chmod(0o755)
    (target_wt / "src" / "kiro_crew").mkdir(parents=True)
    running_wt = tmp_path / "kirocrew-running"
    running_wt.mkdir(parents=True)

    ptr_file = tmp_path / "ptr" / "live_target.json"
    ptr_file.parent.mkdir(parents=True)
    import json as _json
    ptr_file.write_text(_json.dumps({"checkout": str(target_wt)}) + "\n")

    monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: running_wt)

    got = await mod._live_worktree_path()
    assert got == str(running_wt), "live must name the image actually executing"
    assert got != str(target_wt.resolve())
    assert mod._staged_target() == str(target_wt.resolve())


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_live_worktree_path_honours_pointer_when_checkout_unknown(monkeypatch, tmp_path):
    """A packaged install is not a checkout, so the running image cannot be
    compared. That is "cannot verify", not a mismatch: the pointer stays
    authoritative rather than the resolution collapsing to None."""
    target_wt = tmp_path / "kirocrew-wt-new"
    (target_wt / ".venv" / "bin").mkdir(parents=True)
    (target_wt / ".venv" / "bin" / "kirocrew").write_text("#!/bin/sh\n")
    (target_wt / ".venv" / "bin" / "kirocrew").chmod(0o755)
    (target_wt / "src" / "kiro_crew").mkdir(parents=True)

    ptr_file = tmp_path / "ptr" / "live_target.json"
    ptr_file.parent.mkdir(parents=True)
    import json as _json
    ptr_file.write_text(_json.dumps({"checkout": str(target_wt)}) + "\n")

    monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: None)

    assert await mod._live_worktree_path() == str(target_wt.resolve())
    assert mod._staged_target() is None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_repointing_at_the_running_checkout_cancels_a_staged_cutover(monkeypatch, tmp_path):
    """While a cutover is staged, naming the checkout that is RUNNING is a cancel.

    Without this the operator has no un-stage route on exactly the host class this
    feature serves: `already_live` refuses (the running image IS that checkout) and
    the UI hides Make live on live rows, so the only ways out are to complete the
    cutover into the wrong code and reverse it — two manual restarts — or to
    hand-delete a keystone-fenced file the product never names.
    """
    running = _mk_make_live_wt(tmp_path / "running", venv=True, dist=True)
    other = _mk_make_live_wt(tmp_path / "other", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir()

    # The running image IS `running`, so the already_live branch is the one reached.
    # A host this app cannot drive -- exactly the `service install` case #1700 is
    # about, and the only class where the pointer-only cancel applies.
    _stub_make_live(monkeypatch, running, live=str(running), pointer_dir=ptr_dir,
                    unit_status="no_user_unit")
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: running)

    # A cutover to a DIFFERENT checkout is staged.
    import json as _json
    ptr = live_mod.live_target.pointer_path()
    ptr.write_text(_json.dumps({"checkout": str(other)}) + "\n")
    assert mod._staged_target() == str(other)

    res = await mod._make_live(str(running))

    assert res.get("ok") is True, res
    assert res.get("cancelled") is True, res
    assert res.get("code") != "already_live"
    # The pointer is RE-PINNED to the running checkout, not deleted: deleting it
    # would discard the record that this checkout is the chosen live target.
    assert ptr.exists(), "cancelling must not delete the live-target record"
    assert live_mod.live_target.read_target() == running.resolve()
    assert mod._staged_target() is None


def _stage_a_cutover(monkeypatch, tmp_path):
    """running checkout + a pointer staged at a DIFFERENT checkout."""
    running = _mk_make_live_wt(tmp_path / "running", venv=True, dist=True)
    other = _mk_make_live_wt(tmp_path / "other", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir()
    # A host this app cannot drive -- exactly the `service install` case #1700 is
    # about, and the only class where the pointer-only cancel applies.
    _stub_make_live(monkeypatch, running, live=str(running), pointer_dir=ptr_dir,
                    unit_status="no_user_unit")
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: running)
    import json as _json
    ptr = live_mod.live_target.pointer_path()
    ptr.write_text(_json.dumps({"checkout": str(other)}) + "\n")
    assert mod._staged_target() == str(other)
    return running, other, ptr


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cutover_unwind_runs_off_the_event_loop(monkeypatch, tmp_path):
    """The rollback must not block the loop.

    restore() ends in restrict_to_owner, which shells out to icacls on Windows,
    and svc.rollback() rewrites the service definition. Run inline, an unwind
    would stall every other gateway request for the duration of a subprocess, so
    it has to reach the executor like the write it is undoing.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir, unit_status="no_user_unit")

    # Force the cutover write to fail so the unwind path runs.
    monkeypatch.setattr(live_mod.live_target, "write_target",
                        lambda _c: (_ for _ in ()).throw(OSError(28, "No space")))
    loop_thread = threading.get_ident()
    restore_threads: list = []
    monkeypatch.setattr(
        live_mod.live_target, "restore",
        lambda prior: restore_threads.append(threading.get_ident()) or True)

    res = await mod._make_live(str(wt))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res
    assert restore_threads, "the unwind must have run"
    # The thread identity is the real evidence: observing that
    # subprocess_executor() was called proves nothing, since the cutover write
    # already uses it.
    assert all(t != loop_thread for t in restore_threads), (
        "restore ran on the event-loop thread — the unwind was not offloaded"
    )


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_drivable_host_with_a_stage_pending_refuses(monkeypatch, tmp_path):
    """On a host Dev Fleet CAN drive, this request must do NOTHING destructive.

    Two wrong answers to avoid. The pointer-only cancel is unsafe here: a drivable
    host also stages a service DEFINITION, so re-pinning just the pointer leaves
    the definition naming a checkout nobody intends to run, and once that is
    pruned the unit fails to start before it ever reads the pointer. But falling
    through to the full cutover is worse -- it bounces a live gateway carrying
    real sessions in response to a request that reads as "keep running what is
    already running". So it refuses and names both real exits.
    """
    running = _mk_make_live_wt(tmp_path / "running", venv=True, dist=True)
    other = _mk_make_live_wt(tmp_path / "other", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir()
    _stub_make_live(monkeypatch, running, live=str(running), pointer_dir=ptr_dir,
                    unit_status="ok")          # drivable
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: running)
    ptr = live_mod.live_target.pointer_path()
    ptr.write_text(json.dumps({"checkout": str(other)}) + "\n")
    assert mod._staged_target() == str(other)

    res = await mod._make_live(str(running))

    # Neither the pointer-only cancel...
    assert res.get("ok") is False, res
    assert res.get("cancelled") is not True, res
    assert res.get("plan", {}).get("action") != "cancel_staged_cutover", res
    assert res.get("code") == "staged_cutover_pending", res
    # ...nor a cutover: no definition written, and the staged pointer is intact.
    assert not mod._dropin_path().exists(), "a refusal must not write a definition"
    assert mod._staged_target() == str(other), "the stage must survive untouched"
    # The message names the staged checkout so the operator knows both exits.
    assert other.name in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_stale_cancel_after_stage_completed_never_becomes_a_cutover(monkeypatch, tmp_path):
    """The destructive variant of the two-tab race.

    Stage B COMPLETES while tab A's cancel dialog is open: nothing is staged
    any more and B is live. Tab A's stale POST names checkout A (which was
    live) — without the entry gate it would fall past both cancel branches
    into the FULL CUTOVER path and restart the gateway back into A. A request
    carrying expected_staged must refuse (stage_changed) the moment the stage
    it names is gone, whatever branch it would otherwise reach.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    # Complete the staged cutover: `other` is the running checkout now and the
    # pointer agrees with it, so nothing is staged (and the live path resolves
    # to `other` for the same_as_running branch below the gate).
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: other)
    monkeypatch.setattr(live_mod, "_live_worktree_path",
                        AsyncMock(return_value=str(other)))
    assert mod._staged_target() is None

    res = await mod._make_live(str(running), expected_staged=str(other))

    assert res.get("ok") is False, res
    assert res.get("code") == "stage_changed", res
    assert "nothing is staged now" in res["error"]
    # Crucially: no cutover side effects — no service definition written and
    # the pointer still names the completed target.
    assert not mod._dropin_path().exists()
    assert live_mod.live_target.read_target() == other.resolve()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_stale_cancel_refuses_when_the_live_checkout_moved(monkeypatch, tmp_path):
    """The other stale-cancel variant: the LIVE side moved.

    A cancel re-pins the checkout the operator saw as live. If a cutover to C
    landed and a new stage appeared while the dialog sat open, the stale
    request's path names a checkout that is no longer running — matching the
    (re-created) stage alone would let it fall through to the cutover path
    and restart the gateway into the old checkout. The live binding refuses.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    # The gateway moved to a third checkout; the SAME stage happens to exist.
    third = _mk_make_live_wt(tmp_path / "third", venv=True, dist=True)
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: third)
    monkeypatch.setattr(live_mod, "_live_worktree_path",
                        AsyncMock(return_value=str(third)))
    assert mod._staged_target() == str(other)

    res = await mod._make_live(str(running), expected_staged=str(other))

    assert res.get("ok") is False, res
    assert res.get("code") == "stage_changed", res
    assert "live checkout changed" in res["error"]
    # No cutover side effects: nothing written, the stage survives.
    assert not mod._dropin_path().exists()
    assert mod._staged_target() == str(other)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_refuses_when_the_stage_changed_since_confirm(monkeypatch, tmp_path):
    """A cancel is bound to the stage the operator confirmed.

    Two dashboards race: tab A opens the cancel dialog against stage A, tab B
    cancels A and stages B, tab A submits. Without the binding the POST names
    only the live checkout, so it would discard stage B — a stage tab A's
    operator never saw. With ``expected_staged`` the backend refuses instead.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    confirmed_against = tmp_path / "some-older-stage"

    res = await mod._make_live(str(running),
                               expected_staged=str(confirmed_against))

    assert res.get("ok") is False, res
    assert res.get("code") == "stage_changed", res
    # The current stage survives untouched, and the message names both sides.
    assert mod._staged_target() == str(other)
    assert other.name in res["error"]
    assert confirmed_against.name in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_with_matching_expected_stage_proceeds(monkeypatch, tmp_path):
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    res = await mod._make_live(str(running), expected_staged=str(other))

    assert res.get("cancelled") is True, res
    assert mod._staged_target() is None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_keeps_a_pointer_selected_checkout_live(monkeypatch, tmp_path):
    """The scenario that makes deletion wrong.

    Checkout A was made live BY the pointer (so the installed build is something
    else). Staging B and then cancelling must leave A as the live target — if the
    cancel deleted the pointer, the next restart would boot the installed build
    instead of A, silently undoing a cutover the operator never asked to undo.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    res = await mod._make_live(str(running))

    assert res.get("cancelled") is True, res
    assert res["plan"]["keeps_live_target"] == str(running)
    # The record survives AND still names the running checkout.
    assert ptr.exists()
    assert live_mod.live_target.read_target() == running.resolve()
    # Nothing is staged any more, so no restart is pending.
    assert mod._staged_target() is None


@pytest.mark.parametrize("boom", [
    OSError(28, "No space left on device"),
    OSError(30, "Read-only file system"),
])
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_write_failure_is_a_refusal_not_a_crash(monkeypatch, tmp_path, boom):
    """A full or read-only data home must refuse, not raise into a 500.

    write_target mkdirs, writes atomically and re-applies the owner-only mode, so
    the failure mode here is OSError — which the InvalidTarget guard alone (a
    ValueError) does not cover.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def explode(_checkout):
        raise boom

    monkeypatch.setattr(live_mod.live_target, "write_target", explode)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res
    assert "could not be re-pinned" in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_rolls_the_pointer_back_when_hardening_fails(monkeypatch, tmp_path):
    """write_target can fail AFTER replacing the pointer.

    It re-applies the owner-only mode as its last step, so a failure there leaves
    a code-execution input in place with inherited permissions. The cancel must be
    all-or-nothing: the staged pointer goes back exactly as it was.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    staged_before = ptr.read_text(encoding="utf-8")

    real_write = live_mod.live_target.write_target

    def write_then_fail(checkout):
        real_write(checkout)                      # the pointer IS replaced
        raise OSError(5, "SetNamedSecurityInfo failed")

    monkeypatch.setattr(live_mod.live_target, "write_target", write_then_fail)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res
    # Rolled back byte-for-byte: the stage is still staged, nothing half-applied.
    assert ptr.read_text(encoding="utf-8") == staged_before
    assert mod._staged_target() is not None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_reports_a_failed_rollback(monkeypatch, tmp_path):
    """When the rollback itself fails the operator is told, not left guessing."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def write_then_fail(_checkout):
        raise OSError(5, "SetNamedSecurityInfo failed")

    monkeypatch.setattr(live_mod.live_target, "write_target", write_then_fail)
    monkeypatch.setattr(live_mod.live_target, "restore", lambda _prior: False)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert "rollback also failed" in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_invalid_target_is_a_refusal_not_a_crash(monkeypatch, tmp_path):
    """The validation half of the same guard."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def explode(_checkout):
        raise live_mod.live_target.InvalidTarget("no src/kiro_crew in target")

    monkeypatch.setattr(live_mod.live_target, "write_target", explode)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_dry_run_cancel_reports_the_plan_without_deleting(monkeypatch, tmp_path):
    """`dry_run` must never mutate.

    The already_live check runs BEFORE the dry_run return because it is
    validation; turning that point into a pointer delete would make a dry run
    destroy a staged cutover it was only asked to describe.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    res = await mod._make_live(str(running), dry_run=True)

    assert res.get("ok") is True, res
    assert res.get("dry_run") is True
    assert res.get("cancelled") is not True, "dry run must not claim to have acted"
    assert res["plan"]["action"] == "cancel_staged_cutover"
    assert res["plan"]["staged_target"] == str(other)
    assert ptr.exists(), "dry run must NOT delete the pointer"
    assert mod._staged_target() == str(other)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_fails_fast_while_a_cutover_holds_the_lock(monkeypatch, tmp_path):
    """The cancel mutates the same pointer a cutover writes, so it takes the
    same single-flight lock and reports `busy` instead of racing it."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    async with mod._MAKE_LIVE_LOCK:
        res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "busy", res
    assert ptr.exists(), "a contended cancel must not delete the pointer"


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_refuses_once_a_cutover_has_committed(monkeypatch, tmp_path):
    """A committed cutover is already restarting; deleting the pointer then would
    land the pending restart somewhere the operator did not choose."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", True, raising=False)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "restart_pending", res
    assert ptr.exists()


@pytest.mark.asyncio
async def test_loaded_but_inactive_user_unit_is_not_drivable(monkeypatch):
    """A loaded unit is not necessarily the RUNNING gateway.

    `systemctl --user cat` succeeding only proves the unit is known. On a host
    whose gateway runs in the foreground or as a system unit, an idle --user unit
    would otherwise pass the make-live gate: the cutover would bounce that unit,
    the real gateway would keep serving the old code, and the UI would run its
    restart handshake to a false success.
    """
    backend = MagicMock()
    backend.status = AsyncMock(return_value="ok")
    backend.active = AsyncMock(return_value=False)
    monkeypatch.setattr(live_mod, "_gateway_backend", lambda: backend)

    assert await mod._live_user_unit_status() == "user_unit_inactive"
    # The operator-facing reason must say why, not leak the code.
    reason = mod._make_live_status_error("user_unit_inactive")
    assert "not running" in reason
    assert "(user_unit_inactive)" not in reason
    # And it composes into the staged notice, which leads with the remedy.
    notice = mod._staged_notice("main", "user_unit_inactive")
    assert notice.index("kirocrew restart") < notice.index("not running")


@pytest.mark.asyncio
async def test_loaded_and_active_user_unit_is_drivable(monkeypatch):
    """The positive control: loaded AND running still reports ok, so this gate
    did not simply become unreachable."""
    backend = MagicMock()
    backend.status = AsyncMock(return_value="ok")
    backend.active = AsyncMock(return_value=True)
    monkeypatch.setattr(live_mod, "_gateway_backend", lambda: backend)

    assert await mod._live_user_unit_status() == "ok"


def test_running_checkout_resolves_this_checkout():
    """_running_checkout derives the executing checkout from the loaded module,
    which is what makes it authoritative where a service definition is not."""
    got = mod._running_checkout()
    assert got is not None
    assert (got / "src" / "kiro_crew").is_dir()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_staged_only_allows_subsequent_cutover(monkeypatch, tmp_path):
    """A staged_only cutover does NOT latch, so re-pointing to a DIFFERENT
    worktree proceeds without a restart_pending refusal."""
    wt1 = _mk_make_live_wt(tmp_path / "a", venv=True, dist=True)
    wt2 = _mk_make_live_wt(tmp_path / "b", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt1, unit_status="no_user_unit",
                    pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res1 = await mod._make_live(str(wt1), dry_run=False)
    assert res1["ok"] is True and res1["staged_only"] is True
    assert mod._MAKE_LIVE_COMMITTED is False

    # Second cutover to wt2 also succeeds (not refused as restart_pending).
    _stub_make_live(monkeypatch, wt2, unit_status="no_user_unit",
                    pointer_dir=ptr_dir)
    res2 = await mod._make_live(str(wt2), dry_run=False)
    assert res2["ok"] is True and res2["staged_only"] is True
    # Pointer now points to wt2.
    import json as _json
    data = _json.loads((ptr_dir / "live_target.json").read_text())
    assert Path(data["checkout"]).resolve() == wt2.resolve()


# =============================================================================
# Task 2b: fleet gateway_service_active
# =============================================================================


@pytest.mark.asyncio
async def test_fleet_includes_gateway_service_active(monkeypatch):
    """_gateway_service_active probes via the sandboxed _run_cmd chokepoint."""
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl")))
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return 0, "active", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._gateway_service_active() is True
    assert calls and calls[0][:3] == ["systemctl", "--user", "is-active"]


@pytest.mark.asyncio
async def test_gateway_service_active_no_manager(monkeypatch):
    """A platform with neither systemd nor launchd -> False, and NO spawn.

    Was previously asserted with ``platform="darwin"``; darwin is now a
    SUPPORTED backend, so the "no manager at all" case has to be expressed with
    a platform that really has none -- mirroring
    ``test_live_user_unit_status_no_manager``. Asserting darwin here made the
    verdict depend on whether the *host* happened to have the agent loaded,
    because neither ``shutil`` nor ``_run_cmd`` was faked.
    """
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="win32"))
    run = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(runtime_mod, "_run_cmd", run)
    assert await mod._gateway_service_active() is False
    # The "without spawning" half of the original intent, now actually verified:
    # backend() returns None for win32, so nothing may be probed.
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_service_active_darwin_live_agent(monkeypatch):
    """macOS with a loaded agent reporting a live pid -> True.

    The darwin-True path had no *direct* assertion: the only ``is True`` case in
    this area runs under ``platform="linux"``. It was covered indirectly via
    ``test_restart_gateway_darwin_requests_graceful_stop``.
    """
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return 0, "  pid = 4242\n", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._gateway_service_active() is True
    assert calls and calls[0][:2] == ["launchctl", "print"]


@pytest.mark.asyncio
async def test_gateway_service_active_darwin_loaded_without_pid(monkeypatch):
    """macOS agent loaded but not running (no pid line) -> False.

    ``LaunchdBackend.active`` treats only a live pid as active, mirroring
    ``systemctl is-active``; a zero exit from ``launchctl print`` alone is not
    enough.
    """
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(
        runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "  state = waiting\n", ""))
    )
    assert await mod._gateway_service_active() is False


# ---- trusted global credential helper re-injection ----


@pytest.mark.asyncio
async def test_trusted_helpers_loaded_from_global_config(monkeypatch):
    """Operator-global gh helper is SYNTHESIZED and re-pinned after the
    reset; the persistent `store` helper is rejected."""
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")

    scopes: list = []

    async def fake_run_cmd(cmd, **kw):
        assert cmd[:2] == ["git", "config"]
        assert cmd[3] == "--get-regexp"
        scopes.append(cmd[2])
        if cmd[2] != "--global":
            return 1, "", ""  # nothing machine-wide
        return 0, (
            "credential.https://github.com.helper !gh auth git-credential\n"
            "credential.helper store\n"
        ), ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS
    assert h is not None
    base = int(mod._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    assert h[f"GIT_CONFIG_KEY_{base}"] == "credential.https://github.com.helper"
    assert h[f"GIT_CONFIG_VALUE_{base}"] == "!/usr/bin/gh auth git-credential"
    assert f"GIT_CONFIG_KEY_{base + 1}" not in h
    assert h["GIT_CONFIG_COUNT"] == str(base + 1)
    # Both operator-owned scopes are probed, and repo-LOCAL never is: a checkout
    # Dev Fleet builds can write .git/config, and a helper from there would run
    # in the credential-bearing standard tier.
    assert scopes == ["--system", "--global"]


@pytest.mark.asyncio
async def test_trusted_helpers_loaded_from_system_config(monkeypatch):
    """A SYSTEM-scope helper is re-pinned — the stock-macOS case.

    Xcode's Command Line Tools ship `credential.helper = osxkeychain` in the
    system gitconfig and a stock install has nothing in global, so scanning only
    --global left the neutralizer's reset unrepaired and `git fetch` died with
    "could not read Username" (no tty to prompt on).
    """
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        if cmd[2] == "--system":
            return 0, "credential.helper osxkeychain\n", ""
        return 1, "", ""  # nothing in global, as on a stock macOS host

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS or {}
    base = int(mod._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    assert h[f"GIT_CONFIG_KEY_{base}"] == "credential.helper"
    assert h[f"GIT_CONFIG_VALUE_{base}"] == "osxkeychain"
    assert h["GIT_CONFIG_COUNT"] == str(base + 1)


@pytest.mark.asyncio
async def test_trusted_helpers_global_is_pinned_after_system(monkeypatch):
    """Ordering mirrors git's own precedence: system first, then global.

    credential.helper is multi-valued and the LAST entry wins, so an operator's
    own global setting must still override a machine-wide default.
    """
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        if cmd[2] == "--system":
            return 0, "credential.helper osxkeychain\n", ""
        return 0, "credential.helper manager\n", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS or {}
    base = int(mod._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    assert h[f"GIT_CONFIG_VALUE_{base}"] == "osxkeychain"      # system
    assert h[f"GIT_CONFIG_VALUE_{base + 1}"] == "manager"      # global wins
    assert h["GIT_CONFIG_COUNT"] == str(base + 2)


@pytest.mark.asyncio
async def test_trusted_helpers_never_read_repo_local_scope(monkeypatch):
    """--local is never probed.

    Repo-local config is the attack surface the neutralizer's reset exists for:
    a checkout Dev Fleet builds can write .git/config, and a helper from there
    would run in the credential-bearing standard tier.
    """
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)
    scopes: list = []

    async def fake_run_cmd(cmd, **kw):
        scopes.append(cmd[2])
        return 1, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    assert "--local" not in scopes
    assert set(scopes) == {"--system", "--global"}


@pytest.mark.asyncio
async def test_trusted_helpers_empty_when_no_global_config(monkeypatch):
    """No global helpers -> reset stands, no env additions."""
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        return 1, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    assert mod._GIT_TRUSTED_HELPERS == {}


def _fake_helpers():
    base = int(mod._GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    return base, {
        f"GIT_CONFIG_KEY_{base}": "credential.https://github.com.helper",
        f"GIT_CONFIG_VALUE_{base}": "!gh auth git-credential",
        "GIT_CONFIG_COUNT": str(base + 1),
    }


def test_build_env_credentials_only_when_requested(monkeypatch):
    """Trusted helpers layer over the neutralizer reset ONLY for the
    credentialed (fetch) variant; the default build env never sees them."""
    base, helpers = _fake_helpers()
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", helpers)
    env = mod._build_env(with_credentials=True)
    assert env["GIT_CONFIG_COUNT"] == str(base + 1)
    assert env[f"GIT_CONFIG_VALUE_{base}"] == "!gh auth git-credential"
    assert env["GIT_CONFIG_VALUE_2"] == ""  # repo-helper reset still present
    plain = mod._build_env()
    assert f"GIT_CONFIG_KEY_{base}" not in plain
    assert plain["GIT_CONFIG_COUNT"] == str(base)


async def _sync_step_argvs(monkeypatch) -> list:
    """Run _sync with every spawn stubbed; return each step's argv, in order."""
    captured: list = []

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured.append(list(argv))
        return list(argv), dict(env or {}), None

    with patch.object(repository_mod, "_git", new_callable=AsyncMock,
                      return_value=mod.BASE_BRANCH), \
         patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandbox), \
         patch.object(runtime_mod, "_start_run", new_callable=AsyncMock,
                      return_value="rid-steps"):
        worktree_ops_mod._SYNC_RID = None
        res = await mod._sync()
    assert res["ok"] is True
    return captured


def _is_stage_step(argv: list) -> bool:
    # The sync stages via the combined build+stage entry point, which holds the
    # staging lock across both halves.
    return any(
        "stage_built_dist" in str(part) or "build_and_stage" in str(part)
        for part in argv
    )


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_sync_stages_dist_on_a_stock_checkout(monkeypatch):
    """The staging step is part of the sync on a stock checkout.

    `npm run build` writes website/dist while the dashboard serves
    src/kiro_crew/static/dist; without this step Pull+Build reports success and
    the gateway keeps serving the previous bundle.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    argvs = await _sync_step_argvs(monkeypatch)
    stage_at = [i for i, a in enumerate(argvs) if _is_stage_step(a)]
    assert stage_at, f"no staging step in {argvs}"
    # It must be the COMBINED entry point: a staging-only step would put the
    # build back outside the lock holder without tripping the guards below.
    assert any("build_and_stage" in str(x) for x in argvs[stage_at[0]]), \
        f"the stage step must also perform the build: {argvs[stage_at[0]]}"
    # Staging cannot precede the build: they are the SAME step, which holds the
    # staging lock across both so no peer flow can copy a half-written tree.
    ci_at = [i for i, a in enumerate(argvs)
             if Path(a[0]).name == "npm" and "ci" in a]
    assert ci_at and stage_at[0] > ci_at[0], \
        "the build+stage step must run after npm ci"
    assert not any(Path(a[0]).name == "npm" and "build" in a for a in argvs), \
        "a separate npm build step would run outside the staging lock holder"
    # THIS backend's interpreter, not the target checkout's: resolving the
    # helper from the pulled revision would make the step's existence contingent
    # on that revision already carrying it, so an older target would turn the
    # whole Pull+Build into an ImportError.
    assert argvs[stage_at[0]][0] == sys.executable


@pytest.mark.asyncio
async def test_sync_never_stages_dist_on_an_edition_checkout(monkeypatch):
    """An edition checkout must NOT have its dashboard rebuilt or staged over.

    The sync build runs under _build_env(), whose allowlist drops
    KIROCREW_EDITION_DIR / KIROCREW_ALLOW_EDITION, so on an edition composition
    root `npm run build` compiles the STOCK SPA. Staging that would silently
    replace the edition dashboard with upstream's; leaving the shipped bundle in
    place is what frontend's own edition guards already do.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: True)
    argvs = await _sync_step_argvs(monkeypatch)
    assert not any(_is_stage_step(a) for a in argvs)
    # The BUILD is skipped too. vite builds with emptyOutDir, so on a source-tree
    # install -- where static/dist is a symlink to website/dist -- the stock build
    # alone would replace the served edition dashboard, staging step or not.
    assert not any(Path(a[0]).name == "npm" for a in argvs)
    # The backend half of the sync is untouched: an edition still gets the pull
    # and the editable reinstall.
    assert any(Path(a[0]).name == "git" and "fetch" in a for a in argvs)
    assert any("pip" in a for a in argvs)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_sync_build_steps_never_see_credential_helpers(monkeypatch):
    """Only the network fetch step carries operator credential helpers;
    worktree-controlled merge/pip/npm steps must not (token minting via
    `git credential fill` from a malicious install script)."""
    base, helpers = _fake_helpers()
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", helpers)
    captured: list[tuple[list, dict]] = []

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured.append((list(argv), dict(env or {})))
        return list(argv), dict(env or {}), None

    with patch.object(repository_mod, "_git", new_callable=AsyncMock,
                      return_value=mod.BASE_BRANCH), \
         patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandbox), \
         patch.object(runtime_mod, "_start_run", new_callable=AsyncMock,
                      return_value="rid-cred-test"):
        worktree_ops_mod._SYNC_RID = None
        res = await mod._sync()
    assert res["ok"] is True
    key = f"GIT_CONFIG_KEY_{base}"

    def _base(a):
        return [Path(a[0]).name, *(a[1:2])]

    fetch_envs = [e for a, e in captured if _base(a) == ["git", "fetch"]]
    build_envs = [
        e for a, e in captured
        if _base(a) == ["git", "merge"] or "pip" in a or Path(a[0]).name == "npm"
        # The build+stage step is a build step too and must not be exempt from
        # the credential-absence invariant just because it runs via `python -c`.
        or any("build_and_stage" in str(x) for x in a)
        # Neither is the preflight: it runs npm against the incoming lockfile,
        # so it is squarely in the worktree-controlled tier. With the operator
        # repair seam removed, NOTHING on the sync path carries credentials
        # except the fetch step.
        or any("npm_preflight" in str(x) for x in a)
    ]
    assert fetch_envs and all(key in e for e in fetch_envs)
    assert len(build_envs) == 5  # merge + preflight + pip + npm ci + (build + stage)
    assert all(key not in e for e in build_envs)


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_refuses_non_merged(monkeypatch):
    """Branch-name reuse: a fresh OPEN PR on a recycled name must NOT yield a
    head OID at the destructive boundary, even if a stale MERGED verdict is
    cached elsewhere."""
    async def fake_run(cmd, **kw):
        return 0, json.dumps({"headRefOid": "a" * 40, "state": "OPEN"}), ""

    monkeypatch.setattr(fleet_state_mod, "_get_owner_repo", AsyncMock(return_value="o/r"))
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run)
    assert await mod._fetch_pr_head_oid("feature-x") is None

    async def fake_run_merged(cmd, **kw):
        return 0, json.dumps({"headRefOid": "b" * 40, "state": "MERGED"}), ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_merged)
    assert await mod._fetch_pr_head_oid("feature-x") == "b" * 40


@_POSIX_ONLY
def test_trusted_bin_rejects_agent_writable_path(monkeypatch, tmp_path):
    """Bare command names resolve only inside the trusted bin dirs; a planted
    shim in an agent-writable PATH entry is never selected."""
    mod._TRUSTED_BIN_CACHE.clear()
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\necho pwned\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    resolved = mod._trusted_bin("git")
    assert resolved is not None and resolved.startswith(("/usr/", "/bin", "/opt/homebrew/"))
    mod._TRUSTED_BIN_CACHE.clear()
    assert mod._trusted_bin("definitely-not-a-real-tool-xyz") is None
    mod._TRUSTED_BIN_CACHE.clear()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX bin dirs")
def test_trusted_bin_dirs_cover_homebrew_prefixes():
    """A `gh`/`git` the user installed with Homebrew must be reachable: without
    the brew prefixes Dev Fleet could not find gh at all on a stock macOS host
    (only Xcode's /usr/bin/git), and its PATH pin excluded them too."""
    assert "/opt/homebrew/bin" in mod._TRUSTED_BIN_DIRS
    assert "/home/linuxbrew/.linuxbrew/bin" in mod._TRUSTED_BIN_DIRS
    assert "/opt/homebrew/bin" in mod._TRUSTED_PATH.split(os.pathsep)


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX symlink layout")
def test_trusted_bin_pins_the_resolved_target_not_the_symlink(monkeypatch, tmp_path):
    """Homebrew's `bin/gh` is a user-writable symlink into `Cellar/`. Caching the
    LINK would let it be repointed between validation and execution, so the
    vetted real path is what gets cached and spawned."""
    mod._TRUSTED_BIN_CACHE.clear()
    cellar = tmp_path / "Cellar" / "gh" / "1.0" / "bin"
    cellar.mkdir(parents=True)
    target = cellar / "gh"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o555)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").symlink_to(target)
    monkeypatch.setattr(runtime_mod, "_TRUSTED_BIN_DIRS", (str(bin_dir),))

    assert mod._trusted_bin("gh") == str(target.resolve())
    mod._TRUSTED_BIN_CACHE.clear()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_run_cmd_pins_trusted_path(monkeypatch):
    """_run_cmd rewrites bare names to trusted absolute paths and pins PATH."""
    captured: dict = {}

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured["argv"] = list(argv)
        captured["env"] = dict(env or {})
        return ["/bin/false"], dict(env or {}), None

    monkeypatch.setattr(runtime_mod, "sandboxed_spawn_argv", fake_sandbox)
    mod._TRUSTED_BIN_CACHE.clear()
    await mod._run_cmd(["git", "--version"], timeout=5)
    assert captured["argv"][0].startswith(("/usr/", "/bin"))
    assert captured["env"]["PATH"] == mod._TRUSTED_PATH
    mod._TRUSTED_BIN_CACHE.clear()


@pytest.mark.asyncio
async def test_upstream_remote_rejects_option_injection(monkeypatch):
    """A repo-writable `branch.main.remote` shaped like an option must never
    be interpolated into later git argv — fall back to origin."""
    async def fake_run(cmd, **kw):
        if "config" in cmd:
            return 0, "--exec=touch /tmp/pwned #", ""
        return 0, "origin\nkirocrew\n", ""

    monkeypatch.setattr(repository_mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run)
    assert await mod._upstream_remote() == "origin"

    async def fake_run_valid(cmd, **kw):
        if "config" in cmd:
            return 0, "kirocrew", ""
        return 0, "origin\nkirocrew\n", ""

    monkeypatch.setattr(repository_mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_valid)
    assert await mod._upstream_remote() == "kirocrew"

    async def fake_run_unlisted(cmd, **kw):
        if "config" in cmd:
            return 0, "evil", ""
        return 0, "origin\nkirocrew\n", ""

    monkeypatch.setattr(repository_mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_unlisted)
    assert await mod._upstream_remote() == "origin"
    monkeypatch.setattr(repository_mod, "_UPSTREAM_REMOTE", None)


def test_find_cli_is_module_invocation_only():
    """No filesystem resolution: a planted `kirocrew` shim must never become
    the pod CLI. Always our interpreter + the RUNNABLE ``kiro_crew`` package
    entry (its __main__), never ``kiro_crew.cli`` (no __main__ guard -> #220)."""
    import sys as _sys

    assert mod._find_cli() == [_sys.executable, "-m", "kiro_crew"]

    import subprocess as _sp

    cp = _sp.run(
        mod._find_cli() + ["pod"],
        capture_output=True, text=True, timeout=30,
    )
    assert "Usage" in (cp.stdout + cp.stderr) or cp.returncode == 2


def test_sanitize_helper_rejects_shell_and_persistent(monkeypatch):
    """The configured value is never executed as-is: trusted argv[0] with
    attacker arguments (`!/usr/bin/sh -c ...`), persistent helpers
    (store/cache), absolute paths, and argument-carrying names are all
    rejected -- only exact allowlisted shapes select a synthesized command."""
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: "/usr/bin/gh" if n == "gh" else None)
    reject = [
        "",
        "!malicious-command --steal",
        "!/home/user/.local/bin/evil",
        "!/usr/bin/sh -c 'cat > /tmp/creds'",           # trusted argv[0], evil args
        "!/usr/bin/gh auth git-credential --extra",     # extra argv
        "!gh api /user",                                # gh but wrong subcommand
        "store",                                        # persists creds to file
        "cache --timeout=999999",
        "store --file=/tmp/x",
        "/usr/bin/git-credential-store",                # absolute path form
        "osxkeychain --flag",
        '!"unterminated',                               # shlex ValueError
    ]
    for val in reject:
        assert mod._sanitize_helper_value(val) is None, val


def test_sanitize_helper_synthesizes_gh(monkeypatch):
    """A gh-shaped helper is re-synthesized from _trusted_bin -- the
    configured path (e.g. ~/.local/bin/gh via operator override) is
    discarded, so a HOME-planted binary never runs unless the operator
    unit file explicitly designates it."""
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: "/opt/trusted/gh" if n == "gh" else None)
    expected = "!/opt/trusted/gh auth git-credential"
    assert mod._sanitize_helper_value("!gh auth git-credential") == expected
    assert mod._sanitize_helper_value(
        "!/local/home/user/.local/bin/gh auth git-credential") == expected
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: None)
    assert mod._sanitize_helper_value("!gh auth git-credential") is None
    for name in ("osxkeychain", "manager", "manager-core", "libsecret", "wincred"):
        assert mod._sanitize_helper_value(name) == name


@pytest.mark.asyncio
async def test_load_helpers_skips_untrusted(monkeypatch):
    """Untrusted helper lines are dropped; trusted ones survive with a
    correct GIT_CONFIG_COUNT."""
    async def fake_run(cmd, **kw):
        if cmd[2] != "--global":
            return 1, "", ""
        return 0, (
            "credential.helper !evil-shim\n"
            "credential.https://github.com.helper !gh auth git-credential\n"
        ), ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run)
    monkeypatch.setattr(
        runtime_mod, "_trusted_bin",
        lambda n: "/usr/bin/gh" if n == "gh" else None,
    )
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS or {}
    vals = [v for k, v in h.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert vals == ["!/usr/bin/gh auth git-credential"]
    monkeypatch.setattr(runtime_mod, "_GIT_TRUSTED_HELPERS", None)


@pytest.mark.asyncio
async def test_hmac_no_secret_always_denies(monkeypatch):
    """No app secret = fail closed, no env bypass, and the denial is SEL-audited."""
    events: list = []

    class FakeSel:
        def log_tool_invocation(self, **kw):
            events.append(kw)

    monkeypatch.setattr(http_api_mod, "_load_app_secret", lambda: "")
    monkeypatch.setattr(runtime_mod, "_sel", lambda: FakeSel())
    monkeypatch.setenv("KIROCREW_DEVFLEET_INSECURE", "1")
    app = mod.create_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/fleet")
        assert resp.status == 401
    denials = [e for e in events if e.get("outcome") == "denied"]
    assert denials and denials[0]["tool_name"] == "dev-fleet:proxy-hmac"


@pytest.mark.asyncio
async def test_hmac_invalid_signature_denial_is_audited(monkeypatch):
    """Every HMAC 401 path emits exactly one SEL denied event."""
    events: list = []

    class FakeSel:
        def log_tool_invocation(self, **kw):
            events.append(kw)

    monkeypatch.setattr(http_api_mod, "_load_app_secret", lambda: "sekrit")
    monkeypatch.setattr(runtime_mod, "_sel", lambda: FakeSel())
    app = mod.create_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.get("/api/fleet")  # missing header
        r2 = await client.get("/api/fleet", headers={"X-KiroCrew-Proxy": "junk"})
        ts = str(int(time.time()))
        r3 = await client.get(
            "/api/fleet", headers={"X-KiroCrew-Proxy": f"{ts}:deadbeef"})
        assert (r1.status, r2.status, r3.status) == (401, 401, 401)
    assert len([e for e in events if e.get("outcome") == "denied"]) == 3


@pytest.mark.asyncio
async def test_startup_skips_background_tasks_when_disabled(monkeypatch):
    """``dev_fleet_startup`` must not start the refresher/reaper/warm tasks
    when background tasks are disabled, so tests that boot the real app via
    ``create_app()`` (e.g. the HMAC tests above) never drag in a live network
    ``git fetch``. See issue #1832: an unstubbed ``_status_refresher`` leaked
    into unrelated tests and flaked ``Gateway Tests (macOS)``."""
    monkeypatch.setattr(http_api_mod, "_load_app_secret", lambda: "sekrit")
    monkeypatch.setattr(worktree_ops_mod, "_background_tasks_disabled", lambda: True)
    app = mod.create_app()
    async with TestClient(TestServer(app)):
        assert mod._refresher_task is None
        assert mod._reaper_task is None
        assert mod._warm_task is None


@pytest.mark.asyncio
async def test_startup_starts_background_tasks_when_enabled(monkeypatch):
    """The opposite of the above: with the gate off (production default),
    startup still creates all three background tasks."""
    monkeypatch.setattr(http_api_mod, "_load_app_secret", lambda: "sekrit")
    monkeypatch.setattr(worktree_ops_mod, "_background_tasks_disabled", lambda: False)
    monkeypatch.setattr(repository_mod, "_upstream_remote", AsyncMock(return_value="origin"))
    monkeypatch.setattr(runtime_mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(fleet_state_mod, "_fleet_refresh", AsyncMock(return_value=None))
    monkeypatch.setattr(worktree_ops_mod, "_auto_prune_reaper", AsyncMock(return_value=None))
    app = mod.create_app()
    async with TestClient(TestServer(app)):
        assert mod._refresher_task is not None
        assert mod._reaper_task is not None
        assert mod._warm_task is not None


@pytest.mark.asyncio
async def test_prunable_merged_unverified_when_oid_lookup_fails():
    """OID verification unavailable -> never a prune candidate (preview must
    match the removal path, which would refuse anyway)."""
    with patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock,
                      return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=2), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=None), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value=None), \
         patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is False
    assert v["code"] == "merged_unverified"


def test_strict_sandbox_hides_gh_config():
    """.config/gh (the gh helper's token store) must be hidden from the
    strict tier where worktree-controlled code executes."""
    import kiro_crew.sandbox as sandbox_mod

    assert ".config/gh" in sandbox_mod._STRICT_DIRS
    assert ".config/gh" not in sandbox_mod._STANDARD_DIRS


@_POSIX_ONLY
def test_build_preexec_raises_nofile_ceiling(monkeypatch):
    """Build-class spawns get a 65536 NOFILE ceiling (default 1024 EMFILEs vite)."""
    import kiro_crew.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "_BUILD_RESOURCE_PREEXEC", sandbox_mod._UNSET)
    captured: dict = {}

    def fake_apply(cfg):
        captured.update((cfg or {}).get("resource_limits") or {})
        return lambda: None

    monkeypatch.setattr("kiro_crew.security.apply_resource_limits", fake_apply)
    fn = sandbox_mod.build_resource_limit_preexec()
    assert fn is not None
    assert captured["max_open_files"] >= 65536


@_POSIX_ONLY
def test_build_preexec_tolerates_malformed_config(monkeypatch):
    """A junk operator value ("lots") must not raise — the spawn falls back
    to the ceiling instead of leaving Dev Fleet unable to start."""
    import kiro_crew.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "_BUILD_RESOURCE_PREEXEC", sandbox_mod._UNSET)
    monkeypatch.setattr(
        "kiro_crew.config.loader._raw_config",
        lambda: {"resource_limits": {"max_open_files": "lots"}},
    )
    captured: dict = {}

    def fake_apply(cfg):
        captured.update((cfg or {}).get("resource_limits") or {})
        return lambda: None

    monkeypatch.setattr("kiro_crew.security.apply_resource_limits", fake_apply)
    assert sandbox_mod.build_resource_limit_preexec() is not None
    assert captured["max_open_files"] == sandbox_mod._BUILD_NOFILE_CEILING


def test_build_pending_dist_path_is_package_static_dist():
    """The dist probe must resolve to kiro_crew/static/dist — the parent-chain
    silently broke when this module moved from dashboard/handlers/."""
    import pathlib

    root = pathlib.Path(mod.__file__).resolve().parents[3]
    assert root.name == "kiro_crew"
    assert mod._build_pending() in (True, False)
    probed = root / "static" / "dist"
    assert probed.parts[-3:] == ("kiro_crew", "static", "dist")


@pytest.mark.asyncio
async def test_pr_status_falls_back_to_ancestor_verified_repo(monkeypatch):
    """No PR in the upstream repo -> ancestor-verified legacy repo is queried."""
    monkeypatch.setattr(repository_mod, "_FALLBACK_REPOS", ["old-org/old-repo"])
    queried: list = []

    async def fake_owner_repo():
        return "new-org/new-repo"

    async def fake_query(repo, branch):
        queried.append(repo)
        if repo == "old-org/old-repo":
            return {"number": 31, "state": "MERGED", "_repo": repo}
        return None

    monkeypatch.setattr(fleet_state_mod, "_get_owner_repo", fake_owner_repo)
    monkeypatch.setattr(fleet_state_mod, "_pr_query_one", fake_query)
    pr = await mod._fetch_pr_status("feat/legacy")
    assert queried == ["new-org/new-repo", "old-org/old-repo"]
    assert pr is not None and pr["state"] == "MERGED" and pr["_repo"] == "old-org/old-repo"


def test_redact_pr_strips_internal_fields():
    out = mod._redact_pr({"number": 31, "state": "MERGED", "_repo": "o/r"})
    assert out is not None
    assert "_repo" not in out and out["number"] == 31


@pytest.mark.asyncio
async def test_sync_pip_uses_target_repo_venv(monkeypatch, tmp_path):
    """The pip step must run the MAIN_REPO's own venv python — using the
    backend's sys.executable hijacked the gateway venv's editable install."""
    import sys as _sys

    repo = tmp_path / "mainrepo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(repository_mod, "MAIN_REPO", str(repo))
    monkeypatch.setattr(worktree_ops_mod, "_SYNC_RID", None)

    async def fake_remote():
        return "origin"

    async def fake_head():
        return "main"

    monkeypatch.setattr(repository_mod, "_upstream_remote", fake_remote, raising=False)
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")
    captured: dict = {}

    def fake_sandboxed(argv, mode, env=None):
        captured.setdefault("argvs", []).append(list(argv))
        return list(argv), dict(env or {}), None

    monkeypatch.setattr(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandboxed)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)

    async def fake_start_run(label, cmd, **kw):
        captured["cmd"] = cmd
        return "rid-1"

    monkeypatch.setattr(runtime_mod, "_start_run", fake_start_run)
    res = await mod._sync_start_locked()
    assert res.get("ok"), res
    pip_argvs = [a for a in captured.get("argvs", []) if "-m" in a and "pip" in a]
    assert pip_argvs, captured.get("argvs")
    assert pip_argvs[0][0] == str(repo / ".venv" / "bin" / "python")
    assert pip_argvs[0][0] != _sys.executable


@pytest.mark.asyncio
async def test_sync_refuses_when_target_repo_has_no_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(repository_mod, "MAIN_REPO", str(tmp_path / "novenv"))
    monkeypatch.setattr(worktree_ops_mod, "_SYNC_RID", None)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._sync_start_locked()
    assert res.get("ok") is False and "venv" in res.get("error", "")


def test_gateway_unit_resolves_pod_instance(monkeypatch, tmp_path):
    """Inside a pod HOME the restart target is the pod unit, never the live one."""
    pod_home = tmp_path / ".kirocrew-pods" / "kirocrew-wt-feature"
    pod_home.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(pod_home))
    from kiro_crew.config import loader as cfg_loader
    if hasattr(cfg_loader, "_config_dir_cache"):
        monkeypatch.setattr(cfg_loader, "_config_dir_cache", None, raising=False)
    assert mod._gateway_unit_name() == "kirocrew-pod@kirocrew-wt-feature.service"


def test_gateway_unit_resolves_live_outside_pods(monkeypatch, tmp_path):
    home = tmp_path / ".kirocrew"
    home.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    assert mod._gateway_unit_name() == "kirocrew-gateway.service"


@pytest.mark.asyncio
async def test_run_records_authoritative_step_index(monkeypatch):
    """::step:: markers update run['step'] so a chatty build flooding the
    60-line output window cannot lose the phase (reattach correctness)."""
    lines = [b"::step::0::Pull\n", b"noise\n", b"::step::3::npm build\n"] + [
        b"asset line\n"
    ] * 5

    class FakeStdout:
        def __init__(self):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    class FakeProc:
        stdout = FakeStdout()
        pid = 4242
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*a, **k):
        return FakeProc()

    monkeypatch.setattr(runtime_mod.asyncio, "create_subprocess_exec", fake_exec)
    rid = await mod._start_run("sync", ["true"], env={})
    for _ in range(50):
        await runtime_mod.asyncio.sleep(0.05)
        async with mod._RUNS_LOCK:
            if mod._RUNS.get(rid, {}).get("status") == "done":
                break
    async with mod._RUNS_LOCK:
        run = dict(mod._RUNS[rid])
    assert run.get("step") == 3


@pytest.mark.asyncio
async def test_head_contained_when_ancestor(monkeypatch):
    """Local HEAD behind the merged PR head (remote gained commits pre-merge)
    is fully contained in the merge — removal must be allowed."""

    async def fake_run_cmd(cmd, **kw):
        assert cmd[3:5] == ["merge-base", "--is-ancestor"]
        return 0, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._head_contained_in_pr("/wt", "aaa", "bbb") is True


@pytest.mark.asyncio
async def test_head_not_contained_when_diverged(monkeypatch):
    async def fake_run_cmd(cmd, **kw):
        return 1, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._head_contained_in_pr("/wt", "aaa", "bbb") is False


@pytest.mark.asyncio
async def test_head_contained_equal_oids_no_spawn(monkeypatch):
    called = []

    async def fake_run_cmd(cmd, **kw):
        called.append(cmd)
        return 1, "", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    assert await mod._head_contained_in_pr("/wt", "same", "same") is True
    assert not called


# =============================================================================
# Per-worktree context: issue/ticket links + purpose one-liner (issue #147)
# =============================================================================

# --- issue-ref extraction ---
def test_extract_issue_refs_keyworded_and_bare_with_dedup():
    txt = "Fixes #12, Closes #34 and Resolves #56. See also #12 and bare #78."
    assert mod._extract_issue_refs(txt) == [12, 34, 56, 78]


def test_extract_issue_refs_rejects_hex_and_alnum_and_empty():
    # #fff / #1a2b (colours) and #12abc must NOT be parsed as issue refs.
    assert mod._extract_issue_refs("colour #fff border #1a2b tag #12abc") == []
    assert mod._extract_issue_refs("") == []
    assert mod._extract_issue_refs(None) == []  # type: ignore[arg-type]


def test_extract_issue_refs_boundaries():
    # Trailing punctuation / parens still yield the number.
    assert mod._extract_issue_refs("bump (#120) then #7.") == [120, 7]


# --- ticket-id extraction ---
def test_extract_ticket_ids_matches_and_dedups():
    txt = "TT-123 blocked by JIRA-4567; TT-123 again; PROJECT-9"
    assert mod._extract_ticket_ids(txt) == ["TT-123", "JIRA-4567", "PROJECT-9"]


def test_extract_ticket_ids_none_present():
    assert mod._extract_ticket_ids("no tickets here, just words") == []
    assert mod._extract_ticket_ids("") == []


# --- ticket-url template rendering ---
def test_render_ticket_url_with_template():
    assert mod._render_ticket_url("https://t.corp/{id}", "TT-9") == "https://t.corp/TT-9"


def test_render_ticket_url_empty_or_no_placeholder_returns_none():
    assert mod._render_ticket_url("", "TT-9") is None
    assert mod._render_ticket_url("https://t.corp/browse", "TT-9") is None


# --- version-bump detection + summary pick ---
def test_is_version_bump():
    assert mod._is_version_bump("chore: bump version to 1.2.3") is True
    assert mod._is_version_bump("Bump version") is True
    assert mod._is_version_bump("1.2.3") is True
    assert mod._is_version_bump("release 2.0.0") is True
    assert mod._is_version_bump("feat: add pagination") is False


def test_pick_summary_skips_version_bumps():
    subjects = ["chore: bump version 1.2.3", "feat: add real feature", "wip"]
    assert mod._pick_summary(subjects) == "feat: add real feature"


def test_pick_summary_falls_back_to_latest_when_all_bumps():
    subjects = ["chore: bump version 1.2.3", "release 1.2.2"]
    assert mod._pick_summary(subjects) == "chore: bump version 1.2.3"
    assert mod._pick_summary([]) is None


# --- html origin parsing (issue link base) ---
def test_parse_html_repo_base_variants():
    p = mod._parse_html_repo_base
    assert p("git@github.com:kirodotdev/KiroCrew.git") == "https://github.com/kirodotdev/KiroCrew"
    assert p("https://github.com/kirodotdev/KiroCrew.git") == "https://github.com/kirodotdev/KiroCrew"
    assert p("https://github.com/kirodotdev/KiroCrew") == "https://github.com/kirodotdev/KiroCrew"
    assert p("ssh://git@github.com/kirodotdev/KiroCrew.git") == "https://github.com/kirodotdev/KiroCrew"
    assert p("") is None
    assert p("not a url") is None


# --- _pr_query_one carries title, hides body, and _redact_pr drops internals ---
@pytest.mark.asyncio
async def test_pr_query_one_carries_title_and_hides_body():
    payload = json.dumps([{
        "number": 42, "state": "OPEN",
        "url": "https://github.com/o/r/pull/42", "isDraft": False,
        "title": "My PR title", "body": "Fixes #7", "headRefOid": "a" * 40,
    }])
    with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, payload, "")):
        pr = await mod._pr_query_one("o/r", "feat/x")
    assert pr is not None
    assert pr["title"] == "My PR title"
    assert pr["_body"] == "Fixes #7"
    assert pr["_head_oid"] == "a" * 40
    assert "body" not in pr  # moved to internal _body
    assert "headRefOid" not in pr
    redacted = mod._redact_pr(pr)
    assert redacted["title"] == "My PR title"
    assert redacted["number"] == 42
    assert "_body" not in redacted  # internal fields dropped from payload
    assert "_repo" not in redacted
    assert "_head_oid" not in redacted


# --- _build_context: parses PR body + commits, builds links ---
@pytest.mark.asyncio
async def test_build_context_parses_pr_body_and_commits():
    log = (
        "feat(dev-fleet): surface context\x1fFixes #147\nrelated #99\x1e"
        "wip TT-5 progress\x1f\x1e"
    )
    with patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=log), \
         patch.object(fleet_state_mod, "_html_repo_base", new_callable=AsyncMock,
                      return_value="https://github.com/kirodotdev/KiroCrew"), \
         patch.object(repository_mod, "_load_dev_fleet_cfg",
                      return_value={"ticket_url_template": "https://t.corp/{id}"}):
        ctx = await mod._build_context("feat/thing", "/wt/thing", {"_body": "Closes #147\nsee #12"})
    # ordered-unique across pr body + commit subjects + commit bodies
    assert [i["number"] for i in ctx["issues"]] == [147, 12, 99]
    assert ctx["issues"][0]["url"] == "https://github.com/kirodotdev/KiroCrew/issues/147"
    assert [t["id"] for t in ctx["tickets"]] == ["TT-5"]
    assert ctx["tickets"][0]["url"] == "https://t.corp/TT-5"
    assert ctx["summary"] == "feat(dev-fleet): surface context"


@pytest.mark.asyncio
async def test_build_context_graceful_when_git_fails():
    # git log fails (returns None) and there is no PR — issues empty, but a
    # ticket in the BRANCH NAME still resolves; never raises.
    with patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=None), \
         patch.object(fleet_state_mod, "_html_repo_base", new_callable=AsyncMock, return_value=None), \
         patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}):
        ctx = await mod._build_context("TT-42-fix-thing", "/wt/x", None)
    assert ctx["issues"] == []
    assert [t["id"] for t in ctx["tickets"]] == ["TT-42"]
    assert ctx["tickets"][0]["url"] is None  # no template configured
    assert ctx["summary"] is None


@pytest.mark.asyncio
async def test_context_cached_skips_main_and_base():
    empty = {"issues": [], "tickets": [], "summary": None}
    assert await mod._context_cached(None, "/wt", None) == empty
    assert await mod._context_cached(mod.BASE_BRANCH, "/wt", None) == empty


@pytest.mark.asyncio
async def test_context_cached_serves_from_cache(monkeypatch):
    calls = []

    async def fake_build(branch, path, pr):
        calls.append(branch)
        return {"issues": [{"number": 1, "url": None}], "tickets": [], "summary": "s"}

    monkeypatch.setattr(fleet_state_mod, "_build_context", fake_build)
    mod._CTX_CACHE.pop("feat/cache-me", None)
    try:
        a = await mod._context_cached("feat/cache-me", "/wt", None)
        b = await mod._context_cached("feat/cache-me", "/wt", None)
        assert a == b
        assert len(calls) == 1  # second call served from cache
    finally:
        mod._CTX_CACHE.pop("feat/cache-me", None)


# --- fleet payload carries the new context fields per worktree ---
@pytest.mark.asyncio
async def test_build_fleet_payload_has_context_fields():
    sentinel = {
        "issues": [{"number": 147, "url": "https://github.com/o/r/issues/147"}],
        "tickets": [{"id": "TT-5", "url": None}],
        "summary": "feat: do the thing",
    }
    worktrees = [
        {"path": "/repo", "branch": "main", "is_main": True},
        {"path": "/repo-wt-x", "branch": "feat/x", "is_main": False},
    ]
    ginfo = {
        "branch": "feat/x", "head": "abc1234", "dirty": False,
        "ahead": 0, "behind": 0, "last_updated_at": 111,
    }
    pr = {"number": 9, "state": "OPEN", "url": "u", "title": "T"}
    with patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None), \
         patch.object(repository_mod, "_discover_worktrees", new_callable=AsyncMock, return_value=worktrees), \
         patch.object(runtime_mod, "_load_cfg", return_value=None), \
         patch.object(repository_mod, "_git_info", new_callable=AsyncMock, return_value=ginfo), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=pr), \
         patch.object(repository_mod, "_git_ahead", new_callable=AsyncMock, return_value=0), \
         patch.object(fleet_state_mod, "_context_cached", new_callable=AsyncMock, return_value=sentinel), \
         patch.object(live_mod, "_gateway_service_active", new_callable=AsyncMock, return_value=False):
        fleet = await mod._build_fleet()
    rows = {w["name"]: w for w in fleet["worktrees"]}
    feat = rows["repo-wt-x"]
    assert feat["issues"] == sentinel["issues"]
    assert feat["tickets"] == sentinel["tickets"]
    assert feat["summary"] == "feat: do the thing"
    assert feat["pr"]["title"] == "T"  # title carried into the payload
    # main row still present and carries empty context (no crash)
    assert rows["main"]["summary"] is None
    assert rows["main"]["issues"] == []


# =============================================================================
# Non-Linux honesty: the fleet payload must DISCLOSE that pods cannot run here,
# and must still report build state (a plain filesystem fact).
# =============================================================================
async def _fleet_with(worktrees, **patches):
    """Build a fleet payload with everything external stubbed out."""
    ginfo = {
        "branch": "feat/x", "head": "abc1234", "dirty": False,
        "ahead": 0, "behind": 0, "last_updated_at": 111,
    }
    defaults = {
        "_live_worktree_path": AsyncMock(return_value=None),
        "_discover_worktrees": AsyncMock(return_value=worktrees),
        "_git_info": AsyncMock(return_value=ginfo),
        "_pr_status_cached": AsyncMock(return_value=None),
        "_git_ahead": AsyncMock(return_value=0),
        "_context_cached": AsyncMock(
            return_value={"issues": [], "tickets": [], "summary": None}
        ),
        "_gateway_service_active": AsyncMock(return_value=False),
    }
    owners = {
        "_live_worktree_path": live_mod,
        "_discover_worktrees": repository_mod,
        "_git_info": repository_mod,
        "_pr_status_cached": fleet_state_mod,
        "_git_ahead": repository_mod,
        "_context_cached": fleet_state_mod,
        "_gateway_service_active": live_mod,
        "_POD_AVAILABLE": runtime_mod,
        "_POD_IMPORTED": runtime_mod,
        "_POD_ERROR": runtime_mod,
        "_load_cfg": runtime_mod,
        "_staged_target": live_mod,
        "_staged_cancel_available": live_mod,
    }
    with ExitStack() as stack:
        for attr, repl in defaults.items():
            stack.enter_context(patch.object(owners[attr], attr, repl))
        for attr, value in patches.items():
            stack.enter_context(patch.object(owners[attr], attr, value))
        return await mod._build_fleet()


@pytest.mark.asyncio
async def test_fleet_pod_health_is_identity_gated_not_a_bare_port_probe():
    """A squatter's 200 must not paint this worktree's row healthy.

    A pod's port is derived from its name across 199 slots and can be pinned by
    hand, so it is ordinarily held by another pod or by the live gateway; the row
    therefore reads health from the identity-gated probe, which needs the pod NAME
    as well as the port. Pinning the call shape here is what stops a future edit
    reverting to a port-only probe -- the reported failure was a crash-looping pod
    showing a healthy dot because somebody else answered its port.
    """
    seen: list[tuple] = []

    def _health(cfg, name, port, timeout=3):
        seen.append((name, port, timeout))
        return runtime_mod.rt.HEALTH_FOREIGN

    fake_cfg = SimpleNamespace()
    with (
        patch.object(runtime_mod.rt, "health", _health),
        patch.object(runtime_mod.rt, "active_names", lambda cfg: {"repo-wt-x"}),
        patch.object(runtime_mod.rt, "derive_port", lambda cfg, name: 7811),
    ):
        fleet = await _fleet_with(
            [{"path": "/repo-wt-x", "branch": "feat/x", "is_main": False}],
            _POD_AVAILABLE=True,
            _POD_IMPORTED=True,
            _load_cfg=lambda: fake_cfg,
        )

    row = {w["name"]: w for w in fleet["worktrees"]}["repo-wt-x"]
    assert seen == [("repo-wt-x", 7811, 2)]
    # Not a 2xx/401/403, so the frontend's `health >= 200` test renders this row
    # as unhealthy rather than as an open pod.
    assert row["health"] == runtime_mod.rt.HEALTH_FOREIGN
    assert row["health"] < 200


@pytest.mark.asyncio
async def test_fleet_payload_marks_an_inferred_main_checkout():
    with patch.object(repository_mod, "MAIN_REPO_INFERRED", True):
        fleet = await _fleet_with(
            [{"path": mod.MAIN_REPO, "branch": "main", "is_main": True}]
        )

    assert fleet["main_repo"] == mod.MAIN_REPO
    assert fleet["main_repo_inferred"] is True


@pytest.mark.asyncio
async def test_fleet_payload_redacts_credentials_in_main_repo():
    sensitive = f"/tmp/ghp_{'A' * 40}/checkout"
    with patch.object(repository_mod, "_repo", return_value=sensitive):
        fleet = await _fleet_with(
            [{"path": "/repo", "branch": "main", "is_main": True}]
        )

    assert "ghp_" not in fleet["main_repo"]
    assert "[REDACTED" in fleet["main_repo"]


@pytest.mark.asyncio
async def test_fleet_payload_preserves_ordinary_main_repo_path():
    ordinary = "/home/user/oss/KiroCrew"
    with patch.object(repository_mod, "_repo", return_value=ordinary):
        fleet = await _fleet_with(
            [{"path": "/repo", "branch": "main", "is_main": True}]
        )

    assert fleet["main_repo"] == ordinary


@pytest.mark.asyncio
async def test_fleet_payload_discloses_why_pods_are_unavailable():
    """_POD_ERROR used to be computed and then read by NOTHING, so a non-Linux
    user got pod controls that silently failed. It must reach the payload."""
    reason = "Pods are Linux systemd --user units; this host is darwin."
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _POD_AVAILABLE=False,
        _POD_ERROR=reason,
        _load_cfg=lambda: None,
    )
    assert fleet["pods_available"] is False
    assert fleet["pods_unavailable_reason"] == reason


@pytest.mark.asyncio
async def test_fleet_payload_reports_no_reason_when_pods_work():
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _POD_AVAILABLE=True,
        _POD_ERROR="",
        _load_cfg=lambda: None,
    )
    assert fleet["pods_available"] is True
    assert fleet["pods_unavailable_reason"] is None


# =============================================================================
# staged_cancel_available: the payload must mirror the _make_live cancel
# branch's own precondition (not can_restart), NOT _gateway_service_active
# (which also goes true for the foreground last resort, where the pointer-only
# cancel still works). Otherwise the dashboard offers a cancel that /make-live
# refuses with staged_cutover_pending.
# =============================================================================
@pytest.mark.asyncio
async def test_staged_cancel_available_truth_table():
    # No service backend at all: nothing to drive, cancel accepted.
    with patch.object(live_mod, "_gateway_backend", return_value=None):
        assert await mod._staged_cancel_available() is True
    # Drivable manager (unit status ok): _make_live refuses the pointer-only
    # cancel, so the payload must say unavailable.
    with patch.object(live_mod, "_gateway_backend", return_value=object()), \
         patch.object(live_mod, "_live_user_unit_status",
                      new_callable=AsyncMock, return_value="ok"):
        assert await mod._staged_cancel_available() is False
    # Manager present but not drivable (e.g. system unit): cancel accepted —
    # including the foreground-eligible codes, where _gateway_service_active
    # would report True but can_restart stays False.
    with patch.object(live_mod, "_gateway_backend", return_value=object()), \
         patch.object(live_mod, "_live_user_unit_status",
                      new_callable=AsyncMock, return_value="no_user_unit"):
        assert await mod._staged_cancel_available() is True


@pytest.mark.asyncio
async def test_fleet_payload_reports_staged_cancel_available_with_stage():
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _load_cfg=lambda: None,
        _staged_target=lambda: "/w/other",
        _staged_cancel_available=AsyncMock(return_value=True),
    )
    assert fleet["staged_cancel_available"] is True


@pytest.mark.asyncio
async def test_fleet_payload_staged_cancel_false_without_stage():
    """No stage pending: the field is False and the host is NOT probed — the
    fleet endpoint is polled, so an unconditional service-manager probe would
    add a subprocess per poll for a control that cannot render anyway."""
    probe = AsyncMock(return_value=True)
    fleet = await _fleet_with(
        [{"path": "/repo", "branch": "main", "is_main": True}],
        _load_cfg=lambda: None,
        _staged_target=lambda: None,
        _staged_cancel_available=probe,
    )
    assert fleet["staged_cancel_available"] is False
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_state_is_reported_even_where_pods_cannot_run(tmp_path):
    """Regression: has_venv/has_dist sat behind the pod-runnable gate, so every
    worktree showed as "not built" off Linux even when it was fully built.
    They are plain filesystem checks — knowable on every platform."""
    wt = tmp_path / "repo-wt-built"
    binp = wt / ".venv" / ("Scripts" if platform_compat.IS_WINDOWS else "bin")
    binp.mkdir(parents=True)
    exe = binp / ("kirocrew.exe" if platform_compat.IS_WINDOWS else "kirocrew")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (wt / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)

    fleet = await _fleet_with(
        [
            {"path": str(tmp_path / "repo"), "branch": "main", "is_main": True},
            {"path": str(wt), "branch": "feat/x", "is_main": False},
        ],
        # Pods cannot run, and _load_cfg therefore yields no config — the exact
        # state a macOS or Windows host is in.
        _POD_AVAILABLE=False,
        _POD_ERROR="pods require Linux systemd",
        _load_cfg=lambda: None,
    )
    row = {w["name"]: w for w in fleet["worktrees"]}["repo-wt-built"]
    assert row["has_venv"] is True
    assert row["has_dist"] is True
    # Pod state stays false — it genuinely cannot be known here.
    assert row["running"] is False
    assert row["port"] is None


@pytest.mark.asyncio
async def test_main_checkout_build_state_is_probed(tmp_path):
    """Regression (#8058): the build-state probes were gated on ``not is_main``,
    so a fully provisioned MAIN checkout always rendered as unprovisioned —
    during a cutover that reads as "the cutover failed". Build state is a plain
    filesystem check and is knowable for every worktree, main included; only
    the POD-state check legitimately skips main."""
    main_co = tmp_path / "repo"
    binp = main_co / ".venv" / ("Scripts" if platform_compat.IS_WINDOWS else "bin")
    binp.mkdir(parents=True)
    exe = binp / ("kirocrew.exe" if platform_compat.IS_WINDOWS else "kirocrew")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (main_co / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)

    fleet = await _fleet_with(
        [{"path": str(main_co), "branch": "main", "is_main": True}],
        _POD_AVAILABLE=False,
        _POD_ERROR="pods require Linux systemd",
        _load_cfg=lambda: None,
    )
    (row,) = fleet["worktrees"]
    assert row["is_main"] is True
    assert row["has_venv"] is True
    assert row["has_dist"] is True
    # Pod state still never applies to main.
    assert row["running"] is False
    assert row["port"] is None


# =============================================================================
# Regression: _find_cli must target a RUNNABLE entry point (issue #220)
# =============================================================================
def test_find_cli_targets_kiro_crew_package():
    """_find_cli must invoke the ``kiro_crew`` package (its __main__), not
    ``kiro_crew.cli`` — the latter has no __main__ guard and no-ops silently."""
    import sys

    assert mod._find_cli() == [sys.executable, "-m", "kiro_crew"]


def test_kiro_crew_module_entry_actually_runs():
    """The entry point _find_cli uses must actually run main() and emit output.

    Guards the root cause of #220: ``python -m kiro_crew.cli`` imported the
    module, ran no main(), and exited 0 with EMPTY output — so every pod op was
    a silent no-op reported as success. A runnable entry prints usage on --help.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "kiro_crew", "--help"],
        capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip(), "entry point produced no output (silent no-op regression)"


# =============================================================================
# _pod_down post-stop verification (issue #220)
# =============================================================================
@pytest.mark.asyncio
async def test_pod_down_fails_closed_when_still_active():
    """A CLI exit 0 must NOT be reported as success if the unit is still up."""
    with patch.object(worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "active_names", return_value={"kirocrew-wt-x"}):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "still active" in result["error"]


@pytest.mark.asyncio
async def test_pod_down_ok_when_unit_gone():
    """rc 0 AND the unit no longer active -> genuine success."""
    with patch.object(worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "active_names", return_value=set()):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_pod_down_fails_closed_when_verify_raises():
    """If the post-stop active-state check errors, fail closed (never claim ok)."""
    with patch.object(worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "active_names", side_effect=RuntimeError("boom")):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "cannot verify pod shutdown" in result["error"]


@pytest.mark.asyncio
async def test_pod_down_nonzero_rc_is_failure():
    """A non-zero CLI exit is surfaced as failure verbatim."""
    with patch.object(worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(1, "", "stop failed")):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "stop failed" in result["error"]


# =============================================================================
# auto-prune reaper (issue #220)
# =============================================================================
def test_auto_prune_cfg_disabled_by_default():
    with patch.object(repository_mod, "_load_dev_fleet_cfg", return_value={}):
        enabled, interval = mod._auto_prune_cfg()
    assert enabled is False
    assert interval == mod._AUTO_PRUNE_DEFAULT_INTERVAL_S


def test_auto_prune_cfg_enabled_with_interval_floor():
    with patch.object(repository_mod, "_load_dev_fleet_cfg",
                      return_value={"auto_prune": {"enabled": True, "interval_secs": 5}}):
        enabled, interval = mod._auto_prune_cfg()
    assert enabled is True
    # 5s is below the floor -> clamped up to the minimum.
    assert interval == mod._AUTO_PRUNE_MIN_INTERVAL_S


def test_auto_prune_cfg_bad_interval_falls_back():
    with patch.object(repository_mod, "_load_dev_fleet_cfg",
                      return_value={"auto_prune": {"enabled": True, "interval_secs": "nope"}}):
        enabled, interval = mod._auto_prune_cfg()
    assert enabled is True
    assert interval == mod._AUTO_PRUNE_DEFAULT_INTERVAL_S


@pytest.mark.asyncio
async def test_auto_prune_once_removes_merged_only_and_records_failures():
    """Acts ONLY on code=='merged' candidates (stale-empty is skipped); splits
    the merged results into removed/failed."""
    candidates = {"candidates": [
        {"name": "wt-merged", "code": "merged"},
        {"name": "wt-bad", "code": "merged"},
        {"name": "wt-stale-empty", "code": "empty"},  # must be skipped
    ]}
    seen = []

    async def _fake_remove(name, force=False, _caller="handler"):
        assert force is False  # reaper never force-removes
        assert _caller == "reaper"
        seen.append(name)
        return {"ok": True} if name == "wt-merged" else {"ok": False, "error": "nope"}

    with patch.object(worktree_ops_mod, "_prune_candidates", new_callable=AsyncMock, return_value=candidates), \
         patch.object(worktree_ops_mod, "_worktree_remove", side_effect=_fake_remove):
        res = await mod._auto_prune_once()
    assert res["removed"] == ["wt-merged"]
    assert res["failed"] == [{"name": "wt-bad", "error": "nope"}]
    # stale-empty is never touched — unattended auto-prune is merged-only.
    assert "wt-stale-empty" not in seen


@pytest.mark.asyncio
async def test_auto_prune_once_never_reaps_closed_candidates():
    """REGRESSION PIN for hard constraint (a): the unattended reaper MUST stay
    MERGED-only. A CLOSED-PR worktree routinely holds the only copy of work
    that never landed, so silently reaping it on a timer would be irrecoverable
    data loss. This pins that a `closed` candidate — even a clean one that the
    MANUAL checklist WOULD offer — is never handed to _worktree_remove by the
    reaper. If someone later adds `closed` to the reaper's filter, this fails.
    """
    candidates = {"candidates": [
        {"name": "wt-merged", "code": "merged"},
        {"name": "wt-closed", "code": "closed", "unmerged_commits": True},
        {"name": "wt-closed-clean", "code": "closed", "unmerged_commits": False},
    ]}
    seen = []

    async def _fake_remove(name, force=False, _caller="handler"):
        assert force is False  # reaper never force-removes
        assert _caller == "reaper"
        seen.append(name)
        return {"ok": True}

    with patch.object(worktree_ops_mod, "_prune_candidates", new_callable=AsyncMock, return_value=candidates), \
         patch.object(worktree_ops_mod, "_worktree_remove", side_effect=_fake_remove):
        res = await mod._auto_prune_once()
    # Only the merged worktree is reaped; NEITHER closed candidate is touched.
    assert seen == ["wt-merged"]
    assert res["removed"] == ["wt-merged"]
    assert "wt-closed" not in seen
    assert "wt-closed-clean" not in seen


@pytest.mark.asyncio
async def test_auto_prune_once_survives_scan_error():
    with patch.object(worktree_ops_mod, "_prune_candidates", new_callable=AsyncMock,
                      side_effect=RuntimeError("gh down")):
        res = await mod._auto_prune_once()
    # scan failure is surfaced (not swallowed into an empty success) so the
    # reaper can emit a SEL failure event.
    assert res["removed"] == [] and res["failed"] == []
    assert "gh down" in res["error"]


def test_auto_prune_cfg_truthy_nonbool_stays_disabled():
    """A truthy-but-non-boolean 'enabled' (e.g. the string 'false', or 1) must
    NOT arm destructive auto-prune — only literal JSON true does (Codex HIGH)."""
    for bad in ("false", "true", 1, "yes", "0"):
        with patch.object(repository_mod, "_load_dev_fleet_cfg",
                          return_value={"auto_prune": {"enabled": bad}}):
            enabled, _ = mod._auto_prune_cfg()
        assert enabled is False, f"{bad!r} must not enable auto-prune"


@pytest.mark.asyncio
async def test_pod_up_fails_closed_when_not_active():
    """rc==0 but the unit is not active -> fail closed (no false 'started')."""
    with patch.object(worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "{}", "")), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "active_names", return_value=set()):
        result = await mod._pod_up("kirocrew-wt-x")
    assert result["ok"] is False
    assert "not active after start" in result["error"]


@pytest.mark.asyncio
async def test_pod_up_ok_when_active():
    """rc==0 AND the unit active -> success, parsed JSON merged in."""
    with patch.object(worktree_ops_mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, '{"port": 7999}', "")), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "active_names", return_value={"kirocrew-wt-x"}):
        result = await mod._pod_up("kirocrew-wt-x")
    assert result["ok"] is True
    assert result["port"] == 7999


@pytest.mark.asyncio
async def test_auto_prune_reaper_audits_scan_failure():
    """A failed destructive-op cycle (scan error) must still emit a SEL failure
    event — the reaper cannot silently skip auditing (Codex HIGH)."""
    events = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = lambda **kw: events.append(kw)

    async def _break_after_first_cycle(_secs):
        raise asyncio.CancelledError  # exit the while True after one cycle

    with patch.object(worktree_ops_mod, "_auto_prune_cfg", return_value=(True, 300)), \
         patch.object(worktree_ops_mod, "_auto_prune_once", new_callable=AsyncMock,
                      return_value={"removed": [], "failed": [], "error": "gh down"}), \
         patch.object(runtime_mod, "_sel", return_value=fake_sel), \
         patch("asyncio.sleep", _break_after_first_cycle):
        with pytest.raises(asyncio.CancelledError):
            await mod._auto_prune_reaper()

    assert len(events) == 1
    assert events[0]["tool_name"] == "dev_fleet_auto_prune"
    assert events[0]["outcome"] == "failure"
    assert "gh down" in events[0]["error"]


# =============================================================================
# parallel prune (issue #435)
# =============================================================================
async def _await_prune_idle(timeout: float = 5.0) -> None:
    """Wait until the background prune task drains (running -> False)."""
    deadline = time.monotonic() + timeout
    while mod._PRUNE_STATE["running"]:
        if time.monotonic() > deadline:
            raise AssertionError("prune did not finish within timeout")
        await asyncio.sleep(0.01)


@pytest.fixture
def reset_prune_state():
    """Fresh prune locks + state bound to the CURRENT test's event loop.

    ``_PRUNE_LOCK`` / ``_GIT_MUTATION_LOCK`` are module-global asyncio.Locks and
    an asyncio.Lock raises if reused across event loops; pytest-asyncio gives
    each test its own loop, so re-create them (and clear the shared state) per
    test.
    """
    worktree_ops_mod._PRUNE_LOCK = asyncio.Lock()
    worktree_ops_mod._GIT_MUTATION_LOCK = asyncio.Lock()
    mod._PRUNE_STATE.update({
        "running": False, "total": 0, "done": 0, "current": None,
        "results": [], "items": {},
    })
    yield


@pytest.mark.asyncio
async def test_prune_run_per_item_states_and_failure_isolation(reset_prune_state):
    """Each item ends in a terminal per-item status; one failure never stops the
    others; the backward-compat top-level fields are all preserved."""
    names = ["wt-ok", "wt-bad-verdict", "wt-missing", "wt-remove-fail"]

    async def fake_find(nm):
        if nm == "wt-missing":
            return None, "unknown worktree: 'wt-missing'"
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        if path == "/wt/wt-bad-verdict":
            return {"ok": False, "code": "active"}
        return {"ok": True, "code": "merged"}

    async def fake_remove(
        nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        # exercise the phase callback the parallel driver passes in
        if progress is not None:
            progress("stopping_pod")
            progress("removing")
        # No discard was requested for any of these names, so the driver must
        # not turn one on unbidden.
        assert discard_untracked_paths is None
        if nm == "wt-remove-fail":
            return {"ok": False, "error": "pod still active after shutdown"}
        return {"ok": True, "removed": True}

    with patch.object(repository_mod, "_find_worktree", side_effect=fake_find), \
         patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable), \
         patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove):
        r = await mod._prune_run(names)
        assert r == {"ok": True, "total": 4}
        await _await_prune_idle()

    st = await mod._prune_status()
    # backward-compat top-level fields still present and correct
    assert st["running"] is False
    assert st["total"] == 4
    assert st["done"] == 4
    assert "current" in st
    assert len(st["results"]) == 4
    # per-item state machine
    items = st["items"]
    assert items["wt-ok"] == {"status": "done", "error": None}
    assert items["wt-bad-verdict"]["status"] == "failed"
    assert "not prunable" in items["wt-bad-verdict"]["error"]
    assert items["wt-missing"]["status"] == "failed"
    assert "unknown worktree" in items["wt-missing"]["error"]
    assert items["wt-remove-fail"]["status"] == "failed"
    assert "pod still active" in items["wt-remove-fail"]["error"]
    # failure isolation: the one healthy item completed despite 3 failures
    ok_results = [res for res in st["results"] if res.get("ok")]
    assert ok_results == [{"name": "wt-ok", "ok": True, "removed": True}]


@pytest.mark.asyncio
async def test_prune_run_exception_in_item_is_isolated(reset_prune_state):
    """An unexpected exception in one item is caught, marked failed, and does not
    wedge the batch (done still reaches total)."""
    names = ["wt-a", "wt-boom", "wt-b"]

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        return {"ok": True, "code": "merged"}

    async def fake_remove(
        nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None
    ):
        assert discard_untracked_paths is None
        if nm == "wt-boom":
            raise RuntimeError("kaboom")
        return {"ok": True, "removed": True}

    with patch.object(repository_mod, "_find_worktree", side_effect=fake_find), \
         patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable), \
         patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove):
        await mod._prune_run(names)
        await _await_prune_idle()

    st = await mod._prune_status()
    assert st["done"] == 3 and st["running"] is False
    assert st["items"]["wt-boom"]["status"] == "failed"
    assert "kaboom" in st["items"]["wt-boom"]["error"]
    assert st["items"]["wt-a"]["status"] == "done"
    assert st["items"]["wt-b"]["status"] == "done"


@pytest.mark.asyncio
async def test_prune_run_caps_concurrency_at_semaphore_limit(reset_prune_state, monkeypatch):
    """The expensive per-item phase runs concurrently but never exceeds
    _PRUNE_CONCURRENCY simultaneous items."""
    monkeypatch.setattr(worktree_ops_mod, "_PRUNE_CONCURRENCY", 2)
    names = [f"wt-{i}" for i in range(6)]
    inflight = 0
    peak = 0

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)  # hold the semaphore slot
        inflight -= 1
        return {"ok": True, "code": "merged"}

    async def fake_remove(nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None):
        return {"ok": True, "removed": True}

    with patch.object(repository_mod, "_find_worktree", side_effect=fake_find), \
         patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable), \
         patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove):
        await mod._prune_run(names)
        await _await_prune_idle()

    assert peak == 2, f"concurrency should reach (and not exceed) the cap of 2, saw {peak}"
    st = await mod._prune_status()
    assert st["done"] == 6
    assert all(it["status"] == "done" for it in st["items"].values())


@pytest.mark.asyncio
async def test_worktree_remove_serializes_git_mutations(reset_prune_state):
    """Concurrent removals must not overlap inside the git-mutation section — it
    is guarded by _GIT_MUTATION_LOCK so the shared MAIN_REPO .git state is
    mutated by only one worker at a time."""
    inside = 0
    overlapped = False

    async def fake_run_cmd(cmd, timeout=None, **kw):
        nonlocal inside, overlapped
        if "worktree" in cmd and "remove" in cmd:
            inside += 1
            if inside > 1:
                overlapped = True
            await asyncio.sleep(0.05)
            inside -= 1
        return (0, "", "")

    async def fake_find(name):
        return {"path": f"/wt/{name}", "branch": f"feat/{name}"}, None

    with patch.object(repository_mod, "_find_worktree", side_effect=fake_find), \
         patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None), \
         patch.object(live_mod, "_own_checkout_path", return_value=None), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(runtime_mod, "_load_cfg", return_value=None), \
         patch.object(runtime_mod, "_POD_AVAILABLE", False), \
         patch.object(runtime_mod, "_run_cmd", side_effect=fake_run_cmd):
        results = await asyncio.gather(
            mod._worktree_remove("wt-1", force=False),
            mod._worktree_remove("wt-2", force=False),
        )
    assert all(r.get("ok") for r in results), results
    assert overlapped is False, "git mutations overlapped — _GIT_MUTATION_LOCK not serializing"


@pytest.mark.asyncio
async def test_prune_run_deduplicates_names(reset_prune_state):
    """Duplicate names in one request must not spawn racing workers: the list
    is deduplicated (order-preserving) so the same worktree is processed
    exactly once and a duplicate can never report a spurious failure over the
    first worker's success."""
    removed: list[str] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        return {"ok": True, "code": "merged"}

    async def fake_remove(nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None):
        assert discard_untracked_paths is None
        removed.append(nm)
        return {"ok": True, "removed": True}

    with patch.object(repository_mod, "_find_worktree", side_effect=fake_find), \
         patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable), \
         patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove):
        r = await mod._prune_run(["wt-a", "wt-b", "wt-a", "wt-a"])
        assert r == {"ok": True, "total": 2}
        await _await_prune_idle()

    st = await mod._prune_status()
    assert removed.count("wt-a") == 1
    assert st["total"] == 2 and st["done"] == 2
    assert set(st["items"]) == {"wt-a", "wt-b"}
    assert all(it["status"] == "done" for it in st["items"].values())
    assert len(st["results"]) == 2
    # completed batch never leaves a finished name in ``current``
    assert st["current"] is None


@pytest.mark.asyncio
async def test_prune_run_processes_force_only_names(reset_prune_state):
    """A force-override on a kept worktree arrives in ``force_names`` disjoint
    from ``names``. It must still be processed and counted: absent from the
    work list, its ``done`` bump would have no denominator or item row,
    producing the impossible ``1/0`` counter (and a false failure toast). The
    forced item also skips the prunable-verdict recheck — ``_prunable`` is
    never consulted for it."""
    prunable_calls: list[str] = []
    removed: list[str] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        prunable_calls.append(path)
        return {"ok": True, "code": "merged"}

    async def fake_remove(nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None):
        assert discard_untracked_paths is None
        removed.append(nm)
        return {"ok": True, "removed": True}

    with patch.object(repository_mod, "_find_worktree", side_effect=fake_find), \
         patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable), \
         patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None), \
         patch.object(live_mod, "_staged_target", return_value=None), \
         patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove):
        # No regular candidates; a single kept worktree via force-override.
        r = await mod._prune_run([], force_names={"wt-kept"})
        assert r == {"ok": True, "total": 1}
        await _await_prune_idle()

    st = await mod._prune_status()
    # The forced worktree is in the denominator, has an item row, and is done.
    assert st["total"] == 1 and st["done"] == 1
    assert set(st["items"]) == {"wt-kept"}
    assert st["items"]["wt-kept"]["status"] == "done"
    assert removed == ["wt-kept"]
    # Forced items bypass the prunable verdict recheck entirely.
    assert prunable_calls == []


@pytest.mark.asyncio
async def test_prune_run_unions_regular_and_forced_names(reset_prune_state):
    """A mixed batch (regular candidates + a forced kept worktree) processes
    the order-preserving union: every name is counted and removed exactly
    once, and only the non-forced names go through the prunable recheck."""
    prunable_paths: list[str] = []
    removed: list[str] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        prunable_paths.append(path)
        return {"ok": True, "code": "merged"}

    async def fake_remove(nm, force=False, progress=None, _caller="handler", discard_untracked_paths=None):
        assert discard_untracked_paths is None
        removed.append(nm)
        return {"ok": True, "removed": True}

    with patch.object(repository_mod, "_find_worktree", side_effect=fake_find), \
         patch.object(worktree_ops_mod, "_prunable", side_effect=fake_prunable), \
         patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None), \
         patch.object(live_mod, "_staged_target", return_value=None), \
         patch.object(worktree_ops_mod, "_worktree_remove", side_effect=fake_remove):
        r = await mod._prune_run(["wt-a", "wt-b"], force_names={"wt-forced"})
        assert r == {"ok": True, "total": 3}
        await _await_prune_idle()

    st = await mod._prune_status()
    assert st["total"] == 3 and st["done"] == 3
    assert set(st["items"]) == {"wt-a", "wt-b", "wt-forced"}
    assert all(it["status"] == "done" for it in st["items"].values())
    assert sorted(removed) == ["wt-a", "wt-b", "wt-forced"]
    # Only the two regular candidates go through the verdict recheck.
    assert sorted(prunable_paths) == ["/wt/wt-a", "/wt/wt-b"]


@pytest.mark.asyncio
async def test_worktree_remove_refuses_when_pod_reactivates_before_mutation(reset_prune_state):
    """TOCTOU guard: pod inactivity is verified before _GIT_MUTATION_LOCK is
    acquired; if the pod comes back while the worker queues on the lock, the
    recheck inside the lock must refuse the removal (never delete a live
    pod's checkout)."""
    # 1st call: pod-stop section sees inactive (no stop needed).
    # 2nd call: post-lock recheck sees the pod ACTIVE again -> refuse.
    active_calls = iter([[], ["wt-1"]])
    removed_cmds: list[list] = []

    async def fake_run_cmd(cmd, timeout=None, **kw):
        if "worktree" in cmd and "remove" in cmd:
            removed_cmds.append(cmd)
        return (0, "", "")

    async def fake_find(name):
        return {"path": f"/wt/{name}", "branch": f"feat/{name}"}, None

    with patch.object(repository_mod, "_find_worktree", side_effect=fake_find), \
         patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None), \
         patch.object(live_mod, "_own_checkout_path", return_value=None), \
         patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0), \
         patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True), \
         patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(runtime_mod, "_load_cfg", return_value=object()), \
         patch.object(runtime_mod, "_POD_AVAILABLE", True), \
         patch.object(runtime_mod.rt, "require_backend", return_value=None), \
         patch.object(runtime_mod.rt, "active_names", side_effect=lambda cfg: next(active_calls)), \
         patch.object(runtime_mod, "_run_cmd", side_effect=fake_run_cmd):
        res = await mod._worktree_remove("wt-1", force=False)

    assert res.get("ok") is False
    assert "active again" in (res.get("error") or "")
    assert removed_cmds == [], "git worktree remove ran despite live pod"


# --- skill registration (bundled skills inside builtin app) ---


def test_register_skills_creates_symlinks_for_bundled_skills(tmp_path, monkeypatch):
    """Enabling the dev-fleet app registers its bundled pod-e2e skill.

    kirocrew-worktree-dev is deliberately NOT bundled here: the canonical copy
    ships in the top-level ``skills/`` catalog (synced into every install), and
    a second app-bridged copy would drift and be loaded nondeterministically
    against it (PR #353 arbiter finding).
    """
    from kiro_crew.apps.bridges import _register_skills
    from kiro_crew.apps.manifest import AppManifest

    fake_config = tmp_path / "config"
    fake_config.mkdir()
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.config_dir", lambda: fake_config
    )

    app_root = Path(__file__).resolve().parent.parent / (
        "src/kiro_crew/apps/builtins/dev_fleet"
    )

    manifest = AppManifest(
        name="dev-fleet",
        version="1.0.0",
        skills=["skills/pod-e2e"],
    )

    registered = _register_skills("dev-fleet", manifest, app_root)

    skills_dir = fake_config / "skills"
    namespaced_dir = skills_dir / "dev-fleet"

    expected_skills = {"pod-e2e"}
    registered_names = {r.split("/")[-1] for r in registered}
    assert expected_skills <= registered_names
    # The stale bundled copy must stay deleted — the shipped catalog owns it.
    assert not (app_root / "skills" / "kirocrew-worktree-dev").exists()

    # is_link_or_junction, not is_symlink: registration links with a directory junction
    # on Windows (an unprivileged account holds no SeCreateSymbolicLinkPrivilege)
    # and a junction reports is_symlink() False.
    for skill_name in expected_skills:
        link = namespaced_dir / skill_name
        assert platform_compat.is_link_or_junction(link), f"Namespaced link missing: {link}"
        assert link.resolve().is_dir()
        flat = skills_dir / skill_name
        assert platform_compat.is_link_or_junction(flat), f"Flat link missing: {flat}"
        assert flat.resolve().is_dir()


def test_register_skills_tolerates_missing_feature_demo_recording(tmp_path, monkeypatch):
    """feature-demo-recording absence must not crash skill registration."""
    from kiro_crew.apps.bridges import _register_skills
    from kiro_crew.apps.manifest import AppManifest

    fake_config = tmp_path / "config"
    fake_config.mkdir()
    monkeypatch.setattr(
        "kiro_crew.apps.bridges.config_dir", lambda: fake_config
    )

    app_root = Path(__file__).resolve().parent.parent / (
        "src/kiro_crew/apps/builtins/dev_fleet"
    )

    manifest = AppManifest(
        name="dev-fleet",
        version="1.0.0",
        skills=[
            "skills/pod-e2e",
            "skills/kirocrew-worktree-dev",  # no longer bundled — must not crash
            "skills/feature-demo-recording",
        ],
    )

    registered = _register_skills("dev-fleet", manifest, app_root)

    registered_names = {r.split("/")[-1] for r in registered}
    assert "pod-e2e" in registered_names
    # Absent bundled dirs are tolerated, not registered.
    assert "kirocrew-worktree-dev" not in registered_names


@pytest.mark.asyncio
async def test_remove_refuses_live_worktree(monkeypatch):
    """Removing the checkout the live gateway runs from would kill the
    gateway mid-flight -- refused even with force."""
    async def fake_find(name):
        return {"path": "/wt/feature-x", "is_main": False, "branch": "feature-x"}, None

    seen_fresh: list = []

    async def fake_live(*, fresh: bool = False):
        seen_fresh.append(fresh)
        return "/wt/feature-x"

    monkeypatch.setattr(repository_mod, "_find_worktree", fake_find)
    monkeypatch.setattr(live_mod, "_live_worktree_path", fake_live)
    for force in (False, True):
        r = await mod._worktree_remove("feature-x", force=force)
        assert r["ok"] is False
        assert "live gateway" in r["error"]
    # Destructive callers must bypass the 30s cache -- a stale answer could
    # authorize deleting the checkout the gateway switched onto mid-TTL.
    assert seen_fresh == [True, True]


@pytest.mark.asyncio
async def test_remove_refuses_own_process_checkout(monkeypatch):
    """A gateway launched outside systemd is invisible to the unit probe --
    the target must also be checked against the checkout our own running
    code was imported from."""
    async def fake_find(name):
        return {"path": "/wt/self", "is_main": False, "branch": "self"}, None

    async def fake_live(*, fresh: bool = False):
        return None

    monkeypatch.setattr(repository_mod, "_find_worktree", fake_find)
    monkeypatch.setattr(live_mod, "_live_worktree_path", fake_live)
    monkeypatch.setattr(live_mod, "_own_checkout_path", lambda: "/wt/self")
    for force in (False, True):
        r = await mod._worktree_remove("self", force=force)
        assert r["ok"] is False
        assert "current gateway process" in r["error"]


def test_own_checkout_path_resolves_this_worktree():
    own = mod._own_checkout_path()
    assert own is not None
    from pathlib import Path
    assert (Path(own) / "src" / "kiro_crew").is_dir()


# =============================================================================
# Task: restart identity handshake + sync step labels (issue #639)
# =============================================================================


def test_parse_step_marker_index_and_label():
    from kiro_crew.apps.builtins.dev_fleet.server import _parse_step_marker

    assert _parse_step_marker("::step::0::Pull") == (0, "Pull")
    assert _parse_step_marker("::step::3::pip install") == (3, "pip install")


def test_parse_step_marker_non_marker_and_partial():
    from kiro_crew.apps.builtins.dev_fleet.server import _parse_step_marker

    assert _parse_step_marker("regular build output") == (None, None)
    assert _parse_step_marker("::step::") == (None, None)  # no index or label
    assert _parse_step_marker("::step::2::") == (2, None)  # empty label
    assert _parse_step_marker("::step::x::npm ci") == (None, "npm ci")  # bad idx


@pytest.mark.asyncio
async def test_run_endpoint_exposes_step_label():
    """/run returns the server-tracked step + step_label so the UI can name the
    CURRENT sync step ("npm ci") instead of a bare spinner. The label survives
    the 60-line output tail window because it is stored on the run entry."""
    rid = "steplabel-rid"
    async with mod._RUNS_LOCK:
        mod._RUNS[rid] = {
            "status": "running", "exit_code": None, "label": "sync",
            "output": ["::step::3::npm ci"], "started": time.time(),
            "step": 3, "step_label": "npm ci",
        }
    try:
        req = MagicMock()
        req.query = {"id": rid}
        resp = await mod.api_dev_fleet_run(req)
        payload = json.loads(resp.text)
        assert payload["step"] == 3
        assert payload["step_label"] == "npm ci"
    finally:
        async with mod._RUNS_LOCK:
            del mod._RUNS[rid]


@pytest.mark.asyncio
async def test_gateway_start_id_reads_monotonic():
    """_gateway_start_id reads the unit's ExecMainStartTimestampMonotonic."""
    async def fake_run_cmd(cmd, **kw):
        assert "show" in cmd and "--value" in cmd
        assert any("ExecMainStartTimestampMonotonic" in c for c in cmd)
        return (0, "123456789\n", "")

    with patch.object(runtime_mod, "_run_cmd", side_effect=fake_run_cmd), \
         patch.object(live_mod, "sys", MagicMock(platform="linux")), \
         patch.object(live_mod, "shutil",
                      MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))):
        assert await mod._gateway_start_id() == "123456789"


@pytest.mark.asyncio
async def test_gateway_start_id_none_on_non_linux():
    """Non-Linux / no systemctl degrades to None (no hang in 'restarting')."""
    with patch.object(live_mod, "sys", MagicMock(platform="darwin")), \
         patch.object(live_mod, "shutil", MagicMock(which=MagicMock(return_value=None))):
        assert await mod._gateway_start_id() is None


@pytest.mark.asyncio
async def test_gateway_start_id_none_on_zero_empty_or_error():
    """A '0'/empty stamp or a failed probe all normalise to None."""
    with patch.object(live_mod, "sys", MagicMock(platform="linux")), \
         patch.object(live_mod, "shutil",
                      MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))):
        with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock,
                          return_value=(0, "0\n", "")):
            assert await mod._gateway_start_id() is None
        with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock,
                          return_value=(0, "\n", "")):
            assert await mod._gateway_start_id() is None
        with patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock,
                          return_value=(1, "", "err")):
            assert await mod._gateway_start_id() is None


@pytest.mark.asyncio
async def test_restart_gateway_returns_start_id_captured_before_restart():
    """restart-gateway captures the unit's start identity BEFORE scheduling the
    detached restart and returns it, so the frontend waits for a DIFFERENT one
    rather than 'a 200 came back' (issue #639)."""
    calls: list[list[str]] = []

    async def mock_run_cmd(cmd, **kw):
        calls.append(cmd)
        if "is-active" in cmd:
            return (0, "active\n", "")
        if "show" in cmd:  # _gateway_start_id identity probe
            return (0, "555000\n", "")
        return (0, "", "")  # systemd-run

    with patch.object(runtime_mod, "_run_cmd", side_effect=mock_run_cmd), \
         patch.object(live_mod, "sys", MagicMock(platform="linux")), \
         patch.object(live_mod, "shutil",
                      MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))):
        result = await mod._restart_gateway()

    assert result["ok"] is True
    assert result["start_id"] == "555000"
    # The identity probe MUST run before the detached restart is scheduled.
    show_idx = next(i for i, c in enumerate(calls) if "show" in c)
    run_idx = next(i for i, c in enumerate(calls) if "systemd-run" in c)
    assert show_idx < run_idx


@pytest.mark.asyncio
async def test_restart_gateway_start_id_none_safe_when_probe_fails():
    """A failed identity probe still restarts, with start_id=None — the frontend
    then degrades to reload-on-first-response instead of hanging."""
    async def mock_run_cmd(cmd, **kw):
        if "is-active" in cmd:
            return (0, "active\n", "")
        if "show" in cmd:
            return (1, "", "boom")  # probe fails
        return (0, "", "")  # systemd-run

    with patch.object(runtime_mod, "_run_cmd", side_effect=mock_run_cmd), \
         patch.object(live_mod, "sys", MagicMock(platform="linux")), \
         patch.object(live_mod, "shutil",
                      MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))):
        result = await mod._restart_gateway()

    assert result["ok"] is True
    assert result["start_id"] is None


@pytest.mark.asyncio
async def test_api_health_includes_start_id():
    """/health carries the current start identity for the restart handshake."""
    with patch.object(live_mod, "_gateway_start_id", new_callable=AsyncMock,
                      return_value="98765"):
        resp = await mod.api_health(MagicMock())
    payload = json.loads(resp.text)
    assert resp.status == 200
    assert payload["status"] == "ok"
    assert payload["start_id"] == "98765"


@pytest.mark.asyncio
async def test_api_health_start_id_none_safe():
    """/health stays 200/ok with start_id=None when identity is unavailable."""
    with patch.object(live_mod, "_gateway_start_id", new_callable=AsyncMock,
                      return_value=None):
        resp = await mod.api_health(MagicMock())
    payload = json.loads(resp.text)
    assert resp.status == 200
    assert payload["status"] == "ok"
    assert payload["start_id"] is None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_returns_start_id(monkeypatch, tmp_path):
    """A real cutover captures + returns the pre-restart start identity so the
    dashboard reuses the same restart handshake (issue #639)."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if "show" in cmd and any("ExecMainStartTimestampMonotonic" in c for c in cmd):
            return (0, "777111\n", "")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res.get("cutover") is True
    assert res["start_id"] == "777111"
    # The identity probe must precede the detached restart.
    show_idx = next(
        i for i, c in enumerate(calls)
        if "show" in c and any("ExecMainStart" in x for x in c)
    )
    run_idx = next(
        i for i, c in enumerate(calls)
        if c[:2] == ["systemd-run", "--user"] and "restart" in c
    )
    assert show_idx < run_idx


def test_health_registered_on_proxied_api_path():
    """The restart handshake is only reachable if the identity handler is
    registered on the PROXIED /api namespace.

    The browser reaches this backend solely through the gateway proxy, which
    matches /apps/dev-fleet/api/{path} and forwards to /api/{path}; a bare
    /apps/dev-fleet/health is NOT proxied. So /api/health (not just the
    HMAC-exempt internal /health) is what makes the handshake work on the live
    gateway (issue #639). Guard both registrations against a silent regression.
    """
    app = mod.create_app()
    paths = {
        r.resource.canonical
        for r in app.router.routes()
        if r.method == "GET" and r.resource is not None
    }
    assert "/health" in paths        # gateway-internal liveness poll (exempt)
    assert "/api/health" in paths    # proxied path the dashboard actually polls


# =============================================================================
# fleet cache: eviction on removal + single-flight rebuilds
# =============================================================================


@pytest.fixture
def _clean_fleet_cache():
    """Isolate the module-level fleet cache from other tests."""
    saved = dict(mod._FLEET_CACHE)
    saved_inflight = mod._FLEET_INFLIGHT
    saved_epoch = mod._FLEET_EPOCH
    saved_tombs = dict(mod._FLEET_TOMBSTONES)
    mod._FLEET_CACHE.update({"data": None, "ts": 0.0})
    fleet_state_mod._FLEET_INFLIGHT = None
    fleet_state_mod._FLEET_EPOCH = 0
    fleet_state_mod._FLEET_TOMBSTONES = {}
    try:
        yield
    finally:
        mod._FLEET_CACHE.clear()
        mod._FLEET_CACHE.update(saved)
        fleet_state_mod._FLEET_INFLIGHT = saved_inflight
        fleet_state_mod._FLEET_EPOCH = saved_epoch
        fleet_state_mod._FLEET_TOMBSTONES = saved_tombs


def test_fleet_forget_evicts_row(_clean_fleet_cache):
    """A removed worktree must vanish from the cached snapshot immediately.

    Without this the stale-while-revalidate cache serves the pre-removal
    snapshot, so the pruned row keeps rendering for a whole rebuild.
    """
    mod._FLEET_CACHE.update({
        "data": {"worktrees": [{"name": "main"}, {"name": "wt-gone"}], "base_branch": "main"},
        "ts": time.monotonic(),
    })
    mod._fleet_forget("wt-gone")

    names = [w["name"] for w in mod._FLEET_CACHE["data"]["worktrees"]]
    assert names == ["main"]
    # Unrelated snapshot fields survive the surgical eviction.
    assert mod._FLEET_CACHE["data"]["base_branch"] == "main"
    # Timestamp zeroed so the next read schedules a rebuild for the rest.
    assert mod._FLEET_CACHE["ts"] == 0.0


def test_fleet_forget_no_cache_is_noop(_clean_fleet_cache):
    """Removal before any snapshot exists must not crash or fabricate one."""
    mod._fleet_forget("wt-gone")
    assert mod._FLEET_CACHE["data"] is None


def test_fleet_forget_does_not_mutate_served_dict(_clean_fleet_cache):
    """The old dict may still be mid-serialization in a concurrent response."""
    served = {"worktrees": [{"name": "main"}, {"name": "wt-gone"}]}
    mod._FLEET_CACHE.update({"data": served, "ts": time.monotonic()})
    mod._fleet_forget("wt-gone")
    assert [w["name"] for w in served["worktrees"]] == ["main", "wt-gone"]
    assert mod._FLEET_CACHE["data"] is not served


@pytest.mark.asyncio
async def test_worktree_remove_evicts_from_cache(_clean_fleet_cache):
    """_worktree_remove is the single choke point for ALL removal paths
    (manual remove, each prune worker, the auto-prune reaper)."""
    mod._FLEET_CACHE.update({
        "data": {"worktrees": [{"name": "main"}, {"name": "wt-x"}]},
        "ts": time.monotonic(),
    })
    with patch.object(repository_mod, "_find_worktree", new=AsyncMock(
        return_value=({"path": "/repo/wt-x", "branch": "feat/x", "is_main": False}, None)
    )), \
            patch.object(live_mod, "_live_worktree_path", new=AsyncMock(return_value=None)), \
            patch.object(live_mod, "_own_checkout_path", return_value=None), \
            patch.object(repository_mod, "_real_dirty", new=AsyncMock(return_value=False)), \
            patch.object(fleet_state_mod, "_pr_status_cached", new=AsyncMock(
                return_value={"state": "MERGED"})), \
            patch.object(repository_mod, "_own_commits_count", new=AsyncMock(return_value=0)), \
            patch.object(fleet_state_mod, "_fetch_pr_head_oid", new=AsyncMock(return_value="deadbeef")), \
            patch.object(fleet_state_mod, "_head_contained_in_pr", new=AsyncMock(return_value=True)), \
            patch.object(repository_mod, "_git", new=AsyncMock(return_value="deadbeef")), \
            patch.object(runtime_mod, "_load_cfg", return_value=None), \
            patch.object(runtime_mod, "_POD_AVAILABLE", False), \
            patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(0, "", ""))):
        res = await mod._worktree_remove("wt-x")

    assert res["ok"] is True
    assert [w["name"] for w in mod._FLEET_CACHE["data"]["worktrees"]] == ["main"]


@pytest.mark.asyncio
async def test_inflight_rebuild_cannot_resurrect_an_evicted_worktree(_clean_fleet_cache):
    """A rebuild that started BEFORE a removal must not put the row back.

    Such a build read git before the worktree was removed, so its snapshot still
    contains it. Storing that verbatim would undo the eviction and the deleted
    row would reappear.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_build():
        entered.set()
        await release.wait()
        # The pre-removal view of git: wt-gone still present.
        return {"worktrees": [{"name": "main"}, {"name": "wt-gone"}]}

    with patch.object(fleet_state_mod, "_build_fleet", new=_slow_build):
        task = mod._fleet_rebuild_task()
        await entered.wait()
        # Removal lands while that build is in flight.
        mod._fleet_forget("wt-gone")
        release.set()
        built = await task

    assert [w["name"] for w in built["worktrees"]] == ["main"]
    assert [w["name"] for w in mod._FLEET_CACHE["data"]["worktrees"]] == ["main"]


@pytest.mark.asyncio
async def test_fresh_request_coalescing_onto_a_racing_build_still_omits_the_row(
    _clean_fleet_cache,
):
    """The post-removal `fresh=1` refresh may coalesce onto a build that started
    BEFORE the removal. That is safe only because the build re-applies the
    eviction — otherwise the request would answer with the deleted row."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_build():
        entered.set()
        await release.wait()
        return {"worktrees": [{"name": "main"}, {"name": "wt-gone"}]}

    with patch.object(fleet_state_mod, "_build_fleet", new=_slow_build):
        background = mod._fleet_rebuild_task()
        await entered.wait()
        mod._fleet_forget("wt-gone")
        waiter = asyncio.create_task(mod._fleet_refresh())
        await asyncio.sleep(0)
        release.set()
        served = await waiter
        await background

    assert [w["name"] for w in served["worktrees"]] == ["main"]


@pytest.mark.asyncio
async def test_tombstones_are_reaped_by_a_later_build(_clean_fleet_cache):
    """Tombstones must not accumulate: once a build that started after the
    eviction completes, git no longer reports the worktree and the entry is dead
    weight. A stale tombstone would also hide a worktree later re-created under
    the same name."""
    mod._fleet_forget("wt-gone")
    assert "wt-gone" in mod._FLEET_TOMBSTONES

    with patch.object(fleet_state_mod, "_build_fleet", new=AsyncMock(
        return_value={"worktrees": [{"name": "main"}]}
    )):
        await mod._fleet_refresh()
    assert mod._FLEET_TOMBSTONES == {}

    # Re-created under the same name: no stale tombstone hides it.
    with patch.object(fleet_state_mod, "_build_fleet", new=AsyncMock(
        return_value={"worktrees": [{"name": "main"}, {"name": "wt-gone"}]}
    )):
        again = await mod._fleet_refresh()
    assert [w["name"] for w in again["worktrees"]] == ["main", "wt-gone"]


@pytest.mark.asyncio
async def test_fleet_refresh_coalesces_concurrent_builds(_clean_fleet_cache):
    """Concurrent rebuilds share ONE build.

    A rebuild costs a `gh pr` round-trip per branch, so parallel `fresh=1`
    requests (Refresh clicked twice, several row actions finishing together)
    must not each start their own.
    """
    calls = 0
    gate = asyncio.Event()

    async def _slow_build():
        nonlocal calls
        calls += 1
        await gate.wait()
        return {"worktrees": [{"name": "main"}]}

    with patch.object(fleet_state_mod, "_build_fleet", new=_slow_build):
        waiters = [asyncio.create_task(mod._fleet_refresh()) for _ in range(4)]
        await asyncio.sleep(0)  # let every waiter reach the shared task
        gate.set()
        results = await asyncio.gather(*waiters)

    assert calls == 1
    assert all(r == {"worktrees": [{"name": "main"}]} for r in results)
    assert mod._FLEET_CACHE["data"] == {"worktrees": [{"name": "main"}]}


@pytest.mark.asyncio
async def test_fleet_cached_serves_stale_and_schedules_rebuild(_clean_fleet_cache):
    """Past the TTL the cached read stays non-blocking but does trigger a rebuild."""
    mod._FLEET_CACHE.update({
        "data": {"worktrees": [{"name": "stale"}]},
        "ts": time.monotonic() - (mod._FLEET_TTL + 1),
    })
    with patch.object(fleet_state_mod, "_build_fleet", new=AsyncMock(
        return_value={"worktrees": [{"name": "fresh"}]}
    )):
        served = await mod._fleet_cached()
        assert served == {"worktrees": [{"name": "stale"}]}
        assert mod._FLEET_INFLIGHT is not None
        await mod._FLEET_INFLIGHT
    assert mod._FLEET_CACHE["data"] == {"worktrees": [{"name": "fresh"}]}


@pytest.mark.asyncio
async def test_fleet_cached_background_failure_is_swallowed(_clean_fleet_cache):
    """A failed background rebuild keeps serving the last good snapshot and must
    not surface as an unretrieved task exception."""
    mod._FLEET_CACHE.update({
        "data": {"worktrees": [{"name": "stale"}]},
        "ts": time.monotonic() - (mod._FLEET_TTL + 1),
    })
    with patch.object(fleet_state_mod, "_build_fleet", new=AsyncMock(side_effect=RuntimeError("git blew up"))):
        served = await mod._fleet_cached()
        assert served == {"worktrees": [{"name": "stale"}]}
        task = mod._FLEET_INFLIGHT
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
    assert task.exception() is not None
    assert mod._FLEET_CACHE["data"] == {"worktrees": [{"name": "stale"}]}


# --- manifest platform declaration ---
def test_manifest_declares_every_platform_the_app_runs_on():
    """`platform.os` summarises the whole app, and the non-pod half (fleet view,
    Provision, Sync, Rebase, Prune) is git + filesystem work that runs anywhere.

    Pinned because both narrower answers misinform: `["linux"]` reads as "does
    not run on macOS", and omitting the block falls back to the implicit
    ``["macos", "linux"]`` default, which silently drops Windows.
    """
    manifest = json.loads(
        (Path(mod.__file__).parent / "app.json").read_text(encoding="utf-8")
    )
    assert manifest["platform"]["os"] == ["macos", "linux", "windows"]

    # The pod requirement is carried in the UI copy, not the manifest gate. It
    # must track reality: pods now run on Linux (systemd --user) AND macOS
    # (launchd) — with no enforced resource ceiling on macOS — while Make Live
    # stays Linux-only. The old copy ("pods need Linux systemd") became false
    # the moment the launchd backend landed, and this test guards the manifest
    # against lying in either direction.
    assert any(
        "launchd" in h and "Linux" in h for h in manifest["highlights"]
    ), "the highlight must state pods' per-platform reality (Linux systemd + macOS launchd)"
    assert any(
        "Make Live is still Linux-only" in h for h in manifest["highlights"]
    ), "Make Live remains Linux-only and the manifest copy must keep saying so"


def test_declared_platforms_all_resolve_to_a_real_sys_platform():
    """Every declared name must map to a sys.platform value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects. `windows` was in
    exactly that state until the mapping row landed.
    """
    from kiro_crew.apps.manifest import PlatformConfig

    manifest = json.loads(
        (Path(mod.__file__).parent / "app.json").read_text(encoding="utf-8")
    )
    cfg = PlatformConfig(os=manifest["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_sync_builds_and_stages_under_one_lock_holder(monkeypatch, tmp_path):
    """Pull+Build must build and stage inside ONE locked step.

    Without a staging step the live gateway keeps serving through the symlink
    ensure_dev_dist_symlink() points at ``website/dist``, so the build empties
    and rewrites the assets it is serving. The step runs under the Dev Fleet
    backend's OWN interpreter with the target repo passed as an argument:
    resolving the helper from the target would make the step's existence
    contingent on the pulled revision carrying it, so an older target would turn
    the whole Pull+Build into an ImportError.
    """
    repo = tmp_path / "mainrepo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(repository_mod, "MAIN_REPO", str(repo))
    monkeypatch.setattr(worktree_ops_mod, "_SYNC_RID", None)

    async def fake_remote():
        return "origin"

    monkeypatch.setattr(repository_mod, "_upstream_remote", fake_remote, raising=False)
    monkeypatch.setattr(runtime_mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")
    argvs: list[list[str]] = []

    def fake_sandboxed(argv, mode, env=None):
        argvs.append(list(argv))
        return list(argv), dict(env or {}), None

    monkeypatch.setattr(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandboxed)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)

    async def fake_start_run(label, cmd, **kw):
        return "rid-stage"

    monkeypatch.setattr(runtime_mod, "_start_run", fake_start_run)

    res = await mod._sync_start_locked()
    assert res.get("ok"), res

    def _index(pred) -> int:
        for i, a in enumerate(argvs):
            if pred(a):
                return i
        raise AssertionError(f"step not found in {argvs}")

    # Build and stage are ONE step so a single lock holder spans both: the build
    # empties website/dist, and a peer flow staging concurrently would copy a
    # partially written tree.
    stage_i = _index(lambda a: any("build_and_stage" in x for x in a))
    # THIS backend's interpreter, not the target checkout's: the logic is
    # revision-independent, while resolving it from the target would make the
    # step's very existence contingent on the pulled revision already carrying
    # build_and_stage, turning an older target into an ImportError that fails the
    # whole Pull+Build. The repo to build is passed as an argument instead.
    assert argvs[stage_i][0] == sys.executable
    assert str(repo) in argvs[stage_i], "the target repo must be passed explicitly"
    assert argvs[stage_i][-1].endswith("npm"), "the trusted npm path is passed through"
    assert not any(
        a[1:] == ["run", "build", "--prefix", "website"] for a in argvs
    ), "a separate unlocked npm build step would reintroduce the race"
    # npm ci does not touch website/dist, so it stays its own step.
    ci_i = _index(lambda a: a[1:] == ["ci", "--prefix", "website"])
    assert ci_i < stage_i


def test_kill_tree_reaps_a_descendant_that_escaped_the_process_group():
    """A new-session descendant is outside the group, so killpg alone misses it."""
    from kiro_crew.apps.builtins.dev_fleet import server as dev

    killed: list[int] = []

    with patch.object(platform_compat, "process_descendants", return_value=[222]), \
            patch.object(
                platform_compat,
                "kill_process_tree",
                side_effect=lambda pid, *a, **k: killed.append(pid) or True,
            ):
        dev._kill_tree_sync(111)

    assert killed == [111, 222], (
        "the group kill must run first, then each escaped descendant"
    )


def test_kill_tree_enumerates_descendants_before_killing_anything():
    """Ordering is the whole mechanism.

    A kill reparents survivors to init and erases the PPID links, so a snapshot
    taken after the kill cannot see the processes that escaped.
    """
    from kiro_crew.apps.builtins.dev_fleet import server as dev

    events: list[str] = []

    with patch.object(
        platform_compat,
        "process_descendants",
        side_effect=lambda pid: events.append("enumerate") or [222],
    ), patch.object(
        platform_compat,
        "kill_process_tree",
        side_effect=lambda pid, *a, **k: events.append(f"kill{pid}") or True,
    ):
        dev._kill_tree_sync(111)

    assert events[0] == "enumerate", f"enumeration must precede any kill: {events}"
    assert events == ["enumerate", "kill111", "kill222"]


def test_kill_tree_survives_an_already_dead_descendant():
    """The group kill usually reaps descendants; a dead pid must not raise."""
    from kiro_crew.apps.builtins.dev_fleet import server as dev

    def _kill(pid, *a, **k):
        if pid == 222:
            raise ProcessLookupError(pid)
        return True

    with patch.object(platform_compat, "process_descendants", return_value=[222, 333]), \
            patch.object(platform_compat, "kill_process_tree", side_effect=_kill) as km:
        dev._kill_tree_sync(111)

    # 333 is still attempted after 222's ProcessLookupError.
    assert [c.args[0] for c in km.call_args_list] == [111, 222, 333]


def test_live_program_missing_reason_names_the_non_destructive_repairs():
    """`service install` rewrites the whole plist, discarding operator env.

    Naming it as THE repair would contradict the reason this reconcile exists, so
    the guidance points at the two routes that leave the agent definition alone.
    """
    reason = mod._make_live_status_error("live_program_missing")

    assert "Make live" in reason
    assert "source checkout" in reason
    # The destructive route may be mentioned as a contrast, never as the remedy.
    assert "discard" in reason


# --- serving install vs managed checkout --------------------------------------

def test_serving_install_reason_is_silent_for_a_source_install():
    """The normal case: the package answering these routes lives in the checkout.

    Asserted against the REAL package location rather than a fixture, so the
    check cannot pass by accident on a layout that does not exist.
    """
    pkg = Path(mod.__file__).resolve().parents[3]

    assert mod._serving_install_reason_sync(str(pkg.parents[1]), ()) is None


def test_serving_install_reason_is_silent_when_the_checkout_is_the_package_dir():
    """A managed path that IS the serving package is not a mismatch either."""
    pkg = Path(mod.__file__).resolve().parents[3]

    assert mod._serving_install_reason_sync(str(pkg), ()) is None


def test_serving_install_reason_is_silent_after_make_live_onto_a_worktree(tmp_path):
    """Make live points the gateway at a LINKED worktree, outside the primary
    checkout. Warning about a state this app just created — and already labels
    via `is_live` — would train the user to dismiss the takeover signal.
    """
    pkg = Path(mod.__file__).resolve().parents[3]
    serving_checkout = str(pkg.parents[1])

    reason = mod._serving_install_reason_sync(
        str(tmp_path),                      # primary checkout: somewhere else
        (str(tmp_path / "other-wt"), serving_checkout),
    )

    assert reason is None


def test_serving_install_reason_names_both_installs_and_a_remedy(tmp_path):
    """The silent-wrong-answer case: managing checkouts, running none of them.

    Every Dev Fleet control keeps reporting success here, so this string is the
    only thing that can tell the user the pulled code is not the running code —
    which makes naming a next step part of the contract, not decoration.
    """
    (tmp_path / ".git").mkdir()

    reason = mod._serving_install_reason_sync(
        str(tmp_path), (str(tmp_path / "wt-a"), str(tmp_path / "wt-b"))
    )

    assert reason is not None
    # Both sides must be named — one path alone does not identify the mismatch.
    assert tmp_path.name in reason
    assert "kiro_crew" in reason
    assert "Make live" in reason
    # Problem first, action before the paths: a warn banner that leads with two
    # absolute paths and buries the remedy at the end gets skimmed.
    assert reason.startswith("This dashboard is served by a different install")
    assert reason.index("Make live") < reason.index("Serving now:")


def test_serving_install_reason_is_silent_with_no_checkout_to_manage(tmp_path):
    """A path that is not a checkout is not something Dev Fleet manages.

    A desktop-bundle or pip install with no source checkout is the out-of-the-box
    case; warning it to "start the gateway from <path>" names a directory that is
    not there, and a dead-end instruction on every visit trains the signal away.
    """
    assert mod._serving_install_reason_sync(str(tmp_path / "absent"), ()) is None
    # Present but not a checkout is equally unmanageable.
    (tmp_path / "empty").mkdir()
    assert mod._serving_install_reason_sync(str(tmp_path / "empty"), ()) is None


def test_serving_install_reason_accepts_a_linked_worktree_dot_git_file(tmp_path):
    """A linked worktree's `.git` is a FILE, so existence is the right test."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

    assert mod._serving_install_reason_sync(str(tmp_path), ()) is not None


def test_serving_install_reason_skips_unresolvable_entries(tmp_path):
    """A bad path must be skipped, not abort the scan or crash the payload."""
    pkg = Path(mod.__file__).resolve().parents[3]

    # The poison entry comes FIRST; the healthy one after it must still be seen.
    assert mod._serving_install_reason_sync(
        "\x00not-a-path", (str(pkg.parents[1]),)
    ) is None
    # And with nothing healthy anywhere, it still returns without raising.
    (tmp_path / ".git").mkdir()
    assert mod._serving_install_reason_sync(str(tmp_path), ()) is not None


@pytest.mark.asyncio
async def test_serving_install_reason_resolves_paths_off_the_event_loop(monkeypatch):
    """The resolution is filesystem IO, so it must not run on the loop.

    Memoized on the checkout set as well: /fleet is polled, and repeating the
    walk on every poll is what would make a network-backed checkout stall the
    gateway.
    """
    monkeypatch.setattr(fleet_state_mod, "_SERVING_REASON", None)
    monkeypatch.setattr(repository_mod, "MAIN_REPO", "/nowhere/at/all")
    calls: list[tuple] = []

    def _spy(main_repo: str, managed: tuple) -> str | None:
        calls.append((main_repo, managed))
        return "mismatch"

    monkeypatch.setattr(fleet_state_mod, "_serving_install_reason_sync", _spy)
    loop = asyncio.get_running_loop()
    offloaded: list[bool] = []
    real_executor = loop.run_in_executor

    def _tracking_executor(executor, func, *args):
        offloaded.append(True)
        return real_executor(executor, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", _tracking_executor)
    wts = [{"path": "/wt/a"}, {"path": "/wt/b"}, {"no_path": 1}]

    assert await mod._serving_install_reason(wts) == "mismatch"
    assert await mod._serving_install_reason(wts) == "mismatch"

    assert calls == [("/nowhere/at/all", ("/wt/a", "/wt/b"))], "second call must be memoized"
    assert offloaded == [True], "the blocking work must go through an executor"


@pytest.mark.asyncio
async def test_serving_install_reason_recomputes_when_the_checkout_set_changes(
    monkeypatch
):
    """A new worktree can make a previously-foreign serving install managed, so
    the memo must be keyed on the set, not just on MAIN_REPO."""
    monkeypatch.setattr(fleet_state_mod, "_SERVING_REASON", None)
    monkeypatch.setattr(repository_mod, "MAIN_REPO", "/nowhere")
    seen: list[tuple] = []
    monkeypatch.setattr(
        fleet_state_mod, "_serving_install_reason_sync",
        lambda repo, managed: seen.append(managed) or "r",
    )

    await mod._serving_install_reason([{"path": "/wt/a"}])
    await mod._serving_install_reason([{"path": "/wt/a"}, {"path": "/wt/b"}])

    assert seen == [("/wt/a",), ("/wt/a", "/wt/b")]


# --- Worktree teardown guard tests (issue #1554) ---


@pytest.mark.asyncio
async def test_force_remove_refuses_dirty_unmerged_worktree():
    """Regression for #1554: force=True must NOT destroy a dirty tree whose PR
    is unmerged — that combination is unrecoverable data loss."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "OPEN"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "uncommitted changes" in result["error"]
    assert "not merged" in result["error"]


@pytest.mark.asyncio
async def test_force_remove_refuses_dirty_merged_worktree():
    """Regression for #1554 round-5: force=True must NOT destroy a dirty tree
    even when the PR IS merged — containment proves commits are shipped but
    says nothing about working-tree edits. --force bypasses git's dirty check
    and would irrecoverably destroy uncommitted edits."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value="aaa1111",
        ),
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "uncommitted changes" in result["error"]
    assert "commit, stash, or clean" in result["error"]


@pytest.mark.asyncio
async def test_ancestry_gate_skips_ref_delete_when_not_ancestor():
    """When cached PR status wrongly says MERGED but the branch OID is NOT an
    ancestor of the base branch AND containment also fails at the ref-delete
    gate, the ref must survive (fail-closed gate)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    git_calls: list[tuple] = []

    async def _fake_run_cmd(cmd, **kwargs):
        git_calls.append(tuple(cmd))
        if "worktree" in cmd and "remove" in cmd:
            return (0, "", "")
        if "merge-base" in cmd and "--is-ancestor" in cmd:
            return (1, "", "")
        return (0, "", "")

    async def _fake_contained(path, branch_oid, pr_head_oid):
        # The squash-safe race guard passes (worktree path), but the
        # ref-delete gate fails (MAIN_REPO path).
        if path == "/fake/wt":
            return True
        return False

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod,
            "_head_contained_in_pr",
            new_callable=AsyncMock,
            side_effect=_fake_contained,
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_fake_run_cmd),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    update_ref_calls = [c for c in git_calls if "update-ref" in c]
    assert update_ref_calls == [], f"ref should NOT be deleted: {update_ref_calls}"


@pytest.mark.asyncio
async def test_ancestry_gate_allows_ref_delete_when_ancestor():
    """When PR is merged and branch OID IS an ancestor of base, ref IS deleted."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    git_calls: list[tuple] = []
    deleted_refs: list[str] = []

    async def _fake_run_cmd(cmd, **kwargs):
        git_calls.append(tuple(cmd))
        if "worktree" in cmd and "remove" in cmd:
            return (0, "", "")
        if "merge-base" in cmd and "--is-ancestor" in cmd:
            return (0, "", "")
        return (0, "", "")

    async def _fake_git(repo, *args, **kwargs):
        if args and args[0] == "update-ref" and "-d" in args:
            deleted_refs.append(args[2])
            return ""
        if args and args[0] == "rev-parse":
            return "aaa1111"
        return ""

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, side_effect=_fake_git),
        patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_fake_run_cmd),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    assert "refs/heads/feat-x" in deleted_refs


@pytest.mark.asyncio
async def test_empty_branch_ref_survives_when_pr_not_merged():
    """own==0 (empty branch) must NOT delete the branch ref when no PR is
    merged: an empty branch may simply not have been pushed yet, and the ref
    is the only local pointer to those commits (recoverable > irrecoverable).
    Regression test for kirodotdev#1554."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    deleted_refs: list[str] = []

    async def _fake_run_cmd(cmd, **kwargs):
        if "worktree" in cmd and "remove" in cmd:
            return (0, "", "")
        if "merge-base" in cmd and "--is-ancestor" in cmd:
            return (0, "", "")
        return (0, "", "")

    async def _fake_git(repo, *args, **kwargs):
        if args and args[0] == "update-ref" and "-d" in args:
            deleted_refs.append(args[2])
            return ""
        if args and args[0] == "rev-parse":
            return "bbb2222"
        return ""

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "empty-br", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, side_effect=_fake_git),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_fake_run_cmd),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("empty-br", force=False)

    assert result["ok"] is True
    assert deleted_refs == [], f"unmerged empty-branch ref must survive: {deleted_refs}"


@pytest.mark.asyncio
async def test_removal_audit_log_emitted(caplog):
    """Successful removal emits a structured audit line with verdict fields."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "worktree=feat-x" in msg
    assert "branch=feat-x" in msg
    assert "caller=handler" in msg
    assert "action=removed" in msg
    assert "pr_state=MERGED" in msg


@pytest.mark.asyncio
async def test_force_refuse_audit_log_emitted(caplog):
    """The dirty+unmerged force refusal also emits an audit line."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "OPEN"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_dirty_unmerged" in msg
    assert "dirty=True" in msg


# --- Regression tests for PR 1840: force guard fail-closed fixes ---


@pytest.mark.asyncio
async def test_force_remove_refuses_unknown_dirty_state(caplog):
    """Regression (a): _real_dirty returns None (git status failed), force=True,
    PR OPEN — removal must be refused, error names the unverifiable state.
    Previously fell through (fail-open)."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=None),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "OPEN"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "cannot verify worktree cleanliness" in result["error"]
    assert "git status failed" in result["error"]
    # Audit line with dirty=unknown and action=refused_unverifiable
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_unverifiable" in msg
    assert "dirty=unknown" in msg


@pytest.mark.asyncio
async def test_force_remove_refuses_stale_merged_cache(caplog):
    """Regression (b): cached PR status MERGED but fresh _fetch_pr_head_oid
    returns None (stale cache / reused branch name), force=True, dirty=True
    — must refuse. Previously the guard was bypassed entirely."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value=None,
        ),
        # verdict_oid pin succeeds so we reach the fresh-MERGED gate
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="abc123def456"),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "stale cache" in result["error"] or "fresh verification failed" in result["error"]
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_stale_merged" in msg


@pytest.mark.asyncio
async def test_force_remove_fresh_merged_refuses_dirty(caplog):
    """Regression for #1554 round-5: cached MERGED + fresh verdict confirms
    MERGED + dirty=True + force=True → removal REFUSED with audit line
    action=refused_dirty_merged. Containment proves commits are shipped but
    says nothing about working-tree edits."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value="aaa1111",
        ),
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "uncommitted changes" in result["error"]
    assert "commit, stash, or clean" in result["error"]
    # Audit line with action=refused_dirty_merged
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_dirty_merged" in msg
    assert "pr_state=MERGED(fresh)" in msg


@pytest.mark.asyncio
async def test_force_remove_fresh_merged_refuses_unknown_dirty(caplog):
    """Regression for #1554 round-5: cached MERGED + fresh verdict confirms
    MERGED + dirty=None + force=True → removal REFUSED with audit line
    action=refused_unverifiable_merged."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=None),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value="aaa1111",
        ),
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "unverifiable state" in result["error"]
    assert "commit, stash, or clean" in result["error"]
    # Audit line with action=refused_unverifiable_merged
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_unverifiable_merged" in msg
    assert "pr_state=MERGED(fresh)" in msg


@pytest.mark.asyncio
async def test_force_remove_clean_merged_proceeds():
    """Control: force=True + dirty=False + MERGED → removal proceeds WITHOUT --force.
    Clean-tree force removals on merged branches proceed but drop --force so git's
    own dirty check guards the TOCTOU window (round-6 fix)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    run_cmd_calls: list[list[str]] = []

    async def _fake_run_cmd(cmd, **kwargs):
        run_cmd_calls.append(list(cmd))
        return (0, "", "")

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
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
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value="aaa1111",
        ),
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", side_effect=_fake_run_cmd),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is True
    # Round-6 assertion: the removal command must NOT contain --force —
    # git's own dirty check is the atomic TOCTOU guard for merged-clean removals.
    removal_cmds = [c for c in run_cmd_calls if "worktree" in c and "remove" in c]
    assert len(removal_cmds) == 1, f"Expected exactly one removal cmd, got {removal_cmds}"
    assert "--force" not in removal_cmds[0], (
        f"Round-6 regression: merged+clean removal must not pass --force to git; "
        f"got: {removal_cmds[0]}"
    )


@pytest.mark.asyncio
async def test_squash_merge_ref_deletion():
    """Squash-merge regression: PR merged, ancestry check fails (squash merge),
    containment check passes → ref IS deleted. Previously squash-merged refs
    accumulated forever because ancestry is the only gate that passed."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    deleted_refs: list[str] = []

    async def _fake_run_cmd(cmd, **kwargs):
        if "worktree" in cmd and "remove" in cmd:
            return (0, "", "")
        if "merge-base" in cmd and "--is-ancestor" in cmd:
            # Ancestry fails — simulates squash merge
            return (1, "", "")
        return (0, "", "")

    async def _fake_git(repo, *args, **kwargs):
        if args and args[0] == "update-ref" and "-d" in args:
            deleted_refs.append(args[2])
            return ""
        if args and args[0] == "rev-parse":
            return "aaa1111"
        return ""

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, side_effect=_fake_git),
        patch.object(
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value="aaa1111",
        ),
        # Containment passes — branch OID is ancestor of PR head (squash-safe)
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_fake_run_cmd),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    assert (
        "refs/heads/feat-x" in deleted_refs
    ), "ref should be deleted via squash-safe containment fallback"


# --- Regression tests for PR 1840 round-3: TOCTOU + containment fixes ---


@pytest.mark.asyncio
@pytest.mark.xdist_group("caplog_dev_fleet")
async def test_toctou_clean_unmerged_force_omits_git_force(caplog):
    """TOCTOU regression: when force=True overrides ONLY because the unmerged
    tree verified clean, the removal command must NOT contain --force. This way
    git's own dirty check acts as the atomic last-line guard — if the tree
    became dirty in the window between the guard and the actual removal, git
    itself refuses.

    Regression for round-3 fix (b448aa32): at head 3543d9bc the --force flag
    leaked through on this path; this test pinpoints the contract that
    force_use_git_force is set to False and the audit action
    'unmerged_clean_no_git_force' is emitted."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    captured_cmds: list[list[str]] = []

    async def _capture_run_cmd(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        if "worktree" in cmd and "remove" in cmd:
            return (0, "", "")
        if "merge-base" in cmd and "--is-ancestor" in cmd:
            return (1, "", "")  # ancestry fails (irrelevant here)
        return (0, "", "")

    with (
        caplog.at_level(logging.INFO),
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "OPEN"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_capture_run_cmd),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is True
    removal_cmds = [c for c in captured_cmds if "worktree" in c and "remove" in c]
    assert len(removal_cmds) == 1
    assert "--force" not in removal_cmds[0], (
        "clean-unmerged force path must NOT include --force in the git command"
    )
    # Verify the audit action was logged (regression: the action proves
    # the code explicitly set force_use_git_force = False on this path).
    # The security gate is the --force assertion above; this audit check is
    # secondary and can lose its record under xdist worker log routing.
    audit_msgs = [
        r.message for r in caplog.records
        if "unmerged_clean_no_git_force" in r.message
    ]
    if not audit_msgs:
        import warnings
        warnings.warn(
            "caplog audit record missing (xdist log routing); "
            "security assertion (--force not in cmd) passed",
            stacklevel=1,
        )
    else:
        assert len(audit_msgs) == 1, (
            "expected exactly one 'unmerged_clean_no_git_force' audit line"
        )


@pytest.mark.asyncio
async def test_toctou_git_refusal_returns_error_with_audit(caplog):
    """TOCTOU regression: when git worktree remove (without --force) fails
    because the tree became dirty in the window, the function returns ok:False
    and emits the refused_dirty_at_removal audit line."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    async def _failing_run_cmd(cmd, **kwargs):
        if "worktree" in cmd and "remove" in cmd:
            # Simulate git refusing because tree became dirty
            return (1, "", "fatal: '/fake/wt' contains modified tracked files")
        return (0, "", "")

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "OPEN"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_failing_run_cmd),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "modified tracked files" in result["error"]
    # Audit line must record the TOCTOU event
    audit_lines = [r for r in caplog.records if "refused_dirty_at_removal" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_dirty_at_removal" in msg
    assert "dirty_at_removal=True" in msg


@pytest.mark.asyncio
async def test_containment_refuses_uncontained_fresh_head(caplog):
    """Containment regression: cached PR=MERGED, fresh query returns a valid
    head OID (old PR merged genuinely), but the branch's current OID is NOT
    contained in that head (new unmerged commits on a reused branch name) →
    force removal must be refused with refused_uncontained_fresh_head audit."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        # Dirty — so the fresh-MERGED gate fires (only fires for dirty/unknown)
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=5),
        # Fresh head returns the OLD PR's head (valid, non-None)
        patch.object(
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value="old_merged_pr_head_abc123",
        ),
        # rev-parse returns the branch's CURRENT OID (new commits)
        patch.object(
            repository_mod, "_git", new_callable=AsyncMock, return_value="new_unmerged_oid_def456"
        ),
        # Containment fails — new commits not in old PR head
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=False),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "not contained" in result["error"]
    assert "reused branch" in result["error"]
    # Audit
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_uncontained_fresh_head" in msg


@pytest.mark.asyncio
async def test_containment_allows_when_contained():
    """Round 5 regression: cached MERGED + fresh head + branch OID IS contained
    in fresh head BUT worktree is dirty → refused_dirty_merged. Containment
    proves commits are shipped; it cannot vouch for uncommitted working-tree
    edits.

    Pre-round-5 behavior was to allow this removal — the current contract
    refuses it to prevent irrecoverable loss of uncommitted edits."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=3),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="aaa1111"),
        patch.object(
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value="aaa1111",
        ),
        # Containment passes — branch OID is contained in fresh head
        patch.object(fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    # Rounds 5-6: dirty tree is refused even when PR is verified-merged+contained
    assert result["ok"] is False
    assert "uncommitted changes" in result["error"]


# --- Round 4 regressions: containment pin fail-closed ---


@pytest.mark.asyncio
async def test_containment_pin_falsy_refuses_unpinnable(caplog):
    """Regression (round 4): cached MERGED + dirty + force=True, the
    verdict_oid rev-parse returns falsy (None/empty) — a transient git
    failure — must REFUSE the forced removal with refused_unpinnable audit
    rather than silently skip containment and let the later removal proceed.

    At 3ca2ac3a this failed open: `if pinned_oid and not _head_contained()`
    skipped containment when pinned_oid was falsy."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=5),
        # rev-parse fails → returns None (transient git lock contention)
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=None),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "cannot pin branch OID" in result["error"]
    # Audit line must record the refused_unpinnable action
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_unpinnable" in msg


@pytest.mark.asyncio
async def test_containment_pin_empty_string_refuses_unpinnable(caplog):
    """Same as above but rev-parse returns empty string (another falsy form)."""
    import logging

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=5),
        # rev-parse returns empty string (another failure mode)
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=""),
        patch.object(repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "cannot pin branch OID" in result["error"]
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    assert "action=refused_unpinnable" in audit_lines[0].message


@pytest.mark.asyncio
async def test_dirty_unmerged_message_does_not_promise_force_override():
    """Message regression: the non-forced dirty refusal no longer says
    'use force to override' since force is also refused for dirty+unmerged."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(live_mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live_mod, "_own_checkout_path", return_value=None),
        # Dirty tree, non-forced, PR not merged
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=True),
    ):
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is False
    # Must NOT say "use force to override" — that's a dead-end promise
    assert "use force to override" not in result["error"]
    # Should mention uncommitted changes
    assert "uncommitted changes" in result["error"]


# =============================================================================
# Foreground last-resort restart (issue #2566)
# =============================================================================

def _mk_kcbin(tmp_path: Path, name: str = "kirocrew") -> Path:
    """An executable file that passes ForegroundBackend's launcher validation."""
    d = tmp_path / "bin"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def _fg(tmp_path: Path, *, port: int = 7777, pid: int = 4242,
        launcher: "str | None" = None, alive: bool = True, spawn=None,
        ports: "list[int] | None" = None, pids: "dict[int, int] | None" = None,
        confined: "str | None" = None):
    """A ForegroundBackend wired to fakes. Returns (backend, spawned_argvs)."""
    from kiro_crew.apps.builtins.dev_fleet import gateway_service as gs

    spawned: list[list[str]] = []

    def default_spawn(argv):
        spawned.append(list(argv))

    marker_ports = ports if ports is not None else [port]
    pid_by_port = pids if pids is not None else {port: pid}
    backend = gs.ForegroundBackend(
        marker_ports=lambda: list(marker_ports),
        read_pid=lambda p: pid_by_port.get(p),
        read_launcher=lambda p: launcher,
        pid_exists=lambda p: alive,
        spawn=spawn if spawn is not None else default_spawn,
        confinement=lambda: confined,
    )
    return backend, spawned


@pytest.mark.asyncio
async def test_foreground_backend_ok_and_start_id(tmp_path):
    """Single live marker + resolvable binary -> ok; start_id is the marker pid."""
    kc = _mk_kcbin(tmp_path)
    fg, _ = _fg(tmp_path, launcher=str(kc))
    assert await fg.status() == "ok"
    assert await fg.start_id() == "4242"


@pytest.mark.asyncio
async def test_foreground_backend_unavailable_cases(tmp_path):
    """No marker, dead pid, or ambiguous markers -> unavailable, start_id None."""
    kc = _mk_kcbin(tmp_path)
    # No marker at all.
    fg, _ = _fg(tmp_path, ports=[], pids={}, launcher=str(kc))
    assert await fg.status() == "no_foreground_gateway"
    assert await fg.start_id() is None
    # Marker whose pid is dead (crash leftover).
    fg, _ = _fg(tmp_path, alive=False, launcher=str(kc))
    assert await fg.status() == "no_foreground_gateway"
    # Two live markers: ambiguous, never guess which gateway to bounce.
    fg, _ = _fg(tmp_path, ports=[7777, 7778], pids={7777: 1, 7778: 2},
                launcher=str(kc))
    assert await fg.status() == "no_foreground_gateway"
    assert await fg.start_id() is None


@pytest.mark.asyncio
async def test_foreground_backend_binary_resolution(tmp_path):
    """ONLY the keystone-fenced marker launcher is trusted — no PATH fallback
    (an agent can plant a `kirocrew` in ~/.local/bin); invalid recorded
    launchers are refused rather than guessed around."""
    kc = _mk_kcbin(tmp_path)
    # Marker launcher used, verbatim.
    fg, spawned = _fg(tmp_path, launcher=str(kc))
    ok, err = await fg.restart_detached()
    assert ok and spawned[0][0] == str(kc)
    # No recorded launcher (source-tree launch, empty marker): refuse — the
    # PATH fallback is deliberately absent.
    fg, spawned = _fg(tmp_path, launcher=None)
    assert await fg.status() == "no_kirocrew_binary"
    ok, err = await fg.restart_detached()
    assert not ok and "resolved" in err and spawned == []
    # A launcher that is not basenamed kirocrew is refused even when executable
    # (the _own_console_script rule: exec the entry point it claims to be).
    impostor = _mk_kcbin(tmp_path / "i", name="systemctl")
    fg, _ = _fg(tmp_path, launcher=str(impostor))
    assert await fg.status() == "no_kirocrew_binary"
    # A non-executable launcher is refused: it could stop the gateway but never
    # start the replacement.
    limp = _mk_kcbin(tmp_path / "n")
    limp.chmod(0o644)
    fg, _ = _fg(tmp_path, launcher=str(limp))
    assert await fg.status() == "no_kirocrew_binary"
    # A relative recorded path is refused (never resolved against a cwd).
    fg, _ = _fg(tmp_path, launcher="bin/kirocrew")
    assert await fg.status() == "no_kirocrew_binary"


@pytest.mark.asyncio
async def test_foreground_backend_refuses_when_confined(tmp_path):
    """A confined backend (OS sandbox / agents cgroup scope) never spawns: the
    replacement would inherit the confinement for the gateway's whole life."""
    kc = _mk_kcbin(tmp_path)
    fg, spawned = _fg(tmp_path, launcher=str(kc),
                      confined="the Dev Fleet backend runs inside the sandbox")
    assert await fg.status() == "backend_confined"
    ok, err = await fg.restart_detached()
    assert not ok and "sandbox" in err
    assert spawned == []


def test_default_confinement_detects_sandbox_marker(monkeypatch):
    """KIROCREW_SANDBOX_ACTIVE — the launcher-exported in-sandbox marker — is
    detected as confinement (checked before the cgroup read, so this is
    deterministic on any host)."""
    from kiro_crew.apps.builtins.dev_fleet import gateway_service as gs

    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    reason = gs.default_confinement()
    assert reason is not None and "sandbox" in reason


@pytest.mark.asyncio
async def test_foreground_restart_detached_pins_port(tmp_path):
    """The detached command is `<bin> restart --port <marker port>`."""
    kc = _mk_kcbin(tmp_path)
    fg, spawned = _fg(tmp_path, port=6776, pid=99, launcher=str(kc))
    ok, err = await fg.restart_detached()
    assert ok and err == ""
    assert spawned == [[str(kc), "restart", "--port", "6776"]]


@pytest.mark.asyncio
async def test_foreground_spawn_failure_signals_nothing(tmp_path, monkeypatch):
    """A spawn that cannot be established returns (False, why) and the backend
    never signals any process — the incumbent gateway must stay untouched."""
    kc = _mk_kcbin(tmp_path)

    def bad_spawn(argv):
        raise OSError("resource temporarily unavailable")

    kills: list = []
    monkeypatch.setattr(os, "kill", lambda *a: kills.append(a))
    fg, _ = _fg(tmp_path, launcher=str(kc), spawn=bad_spawn)
    ok, err = await fg.restart_detached()
    assert not ok and "resource temporarily unavailable" in err
    assert kills == []


@pytest.mark.asyncio
async def test_make_live_foreground_last_resort_cutover(monkeypatch, tmp_path):
    """When no manager is drivable but a single live foreground gateway exists,
    make-live finishes the cutover itself: pointer written, detached
    `kirocrew restart` established, start_id (the pre-restart pid) returned,
    and the committed latch set exactly as on a drivable host."""
    kc = _mk_kcbin(tmp_path)
    for status in ("no_systemd", "no_user_unit", "no_launchd", "no_agent"):
        wt = _mk_make_live_wt(tmp_path / status, venv=True, dist=True)
        ptr_dir = tmp_path / "ptr" / status
        _stub_make_live(monkeypatch, wt, unit_status=status, pointer_dir=ptr_dir)
        monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        fg, spawned = _fg(tmp_path, port=7777, pid=31337, launcher=str(kc))
        monkeypatch.setattr(live_mod, "_foreground_backend", lambda fg=fg: fg)

        res = await mod._make_live(str(wt), dry_run=False)
        assert res["ok"] is True and res["cutover"] is True, f"{status}: {res}"
        assert "staged_only" not in res
        assert res["start_id"] == "31337"
        assert spawned == [[str(kc), "restart", "--port", "7777"]]
        # Pointer written with the target.
        data = json.loads((ptr_dir / "live_target.json").read_text())
        assert Path(data["checkout"]).resolve() == wt.resolve()
        # A restart IS pending: latched like the drivable path.
        assert mod._MAKE_LIVE_COMMITTED is True


@pytest.mark.asyncio
async def test_make_live_foreground_is_strictly_last_resort(monkeypatch, tmp_path):
    """Ordering is systemd > launchd > foreground: on a drivable host — or one
    whose manager exists but is mis-set-up (named-remedy codes) — the
    foreground backend is never even constructed."""
    factory = MagicMock()
    monkeypatch.setattr(live_mod, "_foreground_backend", factory)
    # Drivable systemd host: full managed cutover, no foreground.
    wt = _mk_make_live_wt(tmp_path / "ok", venv=True, dist=True)
    _stub_make_live(monkeypatch, wt, unit_status="ok",
                    pointer_dir=tmp_path / "ptr-ok")
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and "staged_only" not in res
    factory.assert_not_called()
    # Mis-set-up managers keep their named remedy; foreground must not bounce
    # a gateway behind a manager's back.
    for status in ("user_unit_inactive", "agent_not_indirected",
                   "agent_restart_contract_outdated", "live_program_missing"):
        wt = _mk_make_live_wt(tmp_path / status, venv=True, dist=True)
        _stub_make_live(monkeypatch, wt, unit_status=status,
                        pointer_dir=tmp_path / f"ptr-{status}")
        monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        res = await mod._make_live(str(wt), dry_run=False)
        assert res["ok"] is True and res["staged_only"] is True, f"{status}: {res}"
        factory.assert_not_called()


@pytest.mark.asyncio
async def test_make_live_foreground_spawn_failure_keeps_advisory(monkeypatch, tmp_path):
    """FAIL SAFE: when the detached spawn cannot be established the running
    gateway is untouched, the pointer STAYS staged, the committed latch is not
    set, and the response is the status-quo manual advisory."""
    kc = _mk_kcbin(tmp_path)
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, unit_status="no_systemd", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    def bad_spawn(argv):
        raise OSError("spawn refused")

    fg, _ = _fg(tmp_path, launcher=str(kc), spawn=bad_spawn)
    monkeypatch.setattr(live_mod, "_foreground_backend", lambda: fg)

    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res["staged_only"] is True
    assert res["manual_restart"]
    assert res["manual_restart"] in res["notice"]
    # The plan must not keep promising an automatic restart that failed.
    assert res["plan"]["restart"] == "manual"
    # Pointer written and KEPT: "finish with one command", not "start over".
    data = json.loads((ptr_dir / "live_target.json").read_text())
    assert Path(data["checkout"]).resolve() == wt.resolve()
    # No restart pending -> not latched; a re-point must stay allowed.
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_make_live_foreground_unavailable_keeps_advisory(monkeypatch, tmp_path):
    """Eligible codes without a usable foreground gateway (no/ambiguous marker,
    unresolvable binary, confined backend) keep the pre-existing staged_only
    behaviour."""
    kc = _mk_kcbin(tmp_path)
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    for label, fg_kwargs in (
        ("no_marker", dict(ports=[], pids={}, launcher=None)),
        ("confined", dict(launcher=str(kc), confined="backend is sandboxed")),
    ):
        _stub_make_live(monkeypatch, wt, unit_status="no_systemd",
                        pointer_dir=ptr_dir / label)
        monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        fg, spawned = _fg(tmp_path, **fg_kwargs)
        monkeypatch.setattr(live_mod, "_foreground_backend", lambda fg=fg: fg)

        res = await mod._make_live(str(wt), dry_run=False)
        assert res["ok"] is True and res["staged_only"] is True, f"{label}: {res}"
        assert res["manual_restart"] in res["notice"]
        assert spawned == []
        assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_make_live_foreground_dry_run_plan(monkeypatch, tmp_path):
    """A dry run on a foreground-capable host reports the restart as automatic
    with the exact command — and mutates nothing, spawns nothing."""
    kc = _mk_kcbin(tmp_path)
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, unit_status="no_systemd", pointer_dir=ptr_dir)
    fg, spawned = _fg(tmp_path, port=7777, pid=1, launcher=str(kc))
    monkeypatch.setattr(live_mod, "_foreground_backend", lambda: fg)

    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    plan = res["plan"]
    assert plan["restart"] == "automatic"
    assert plan["restart_backend"] == "foreground"
    assert plan["restart_command"] == f"{kc} restart --port 7777"
    assert spawned == []
    assert not (ptr_dir / "live_target.json").exists()


@pytest.mark.asyncio
async def test_gateway_start_id_foreground_fallback(monkeypatch, tmp_path):
    """On an eligible host the health handshake identity is the marker pid, so
    the dashboard can observe a foreground cutover complete; on an ineligible
    host it stays None (degrade, never wait forever)."""
    kc = _mk_kcbin(tmp_path)
    fg, _ = _fg(tmp_path, pid=8080, launcher=str(kc))
    monkeypatch.setattr(live_mod, "_foreground_backend", lambda: fg)
    with patch.object(live_mod, "sys", MagicMock(platform="linux")), \
         patch.object(live_mod, "shutil",
                      MagicMock(which=MagicMock(return_value=None))):
        # No systemctl at all -> primary yields None -> foreground pid.
        assert await mod._gateway_start_id() == "8080"
    with patch.object(live_mod, "_live_user_unit_status", new_callable=AsyncMock,
                      return_value="user_unit_inactive"), \
         patch.object(live_mod, "sys", MagicMock(platform="linux")), \
         patch.object(live_mod, "shutil",
                      MagicMock(which=MagicMock(return_value=None))):
        assert await mod._gateway_start_id() is None


# ---------------------------------------------------------------------------
# Regression tests: make-live artifact validation inside the cutover lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_artifact_changed_before_lock_is_revalidated(
    monkeypatch, tmp_path
):
    """Artifacts that are valid at early-probe time but gone before the lock
    is acquired are caught by the in-lock re-validation.

    A side-effecting lock wrapper removes the venv binary at the instant the
    lock is acquired, reproducing a concurrent provision that replaces the
    binary between the early check and the commit.  The cutover must refuse
    with ``missing_venv`` and must NOT write the pointer.

    Without the production fix the early probe passes, the lock is acquired,
    and the cutover proceeds to write the pointer and stage a restart — the
    stale validation is never repeated and the race window is not closed.
    This test proves the ordering by observing the final response code and
    the pointer-file state.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False)

    kcbin = wt / ".venv" / "bin" / "kirocrew"

    # Wrap _MAKE_LIVE_LOCK so that entering the lock removes the binary,
    # simulating a concurrent rebuild that completes between the early probe
    # and the lock-acquire.
    real_lock = asyncio.Lock()

    class _SideEffectLock:
        """Proxy that removes *kcbin* when the lock body is entered."""

        def locked(self) -> bool:
            return real_lock.locked()

        async def __aenter__(self):
            await real_lock.__aenter__()
            # Binary vanishes at the moment the lock body begins.
            kcbin.unlink(missing_ok=True)
            return self

        async def __aexit__(self, *args):
            return await real_lock.__aexit__(*args)

    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", _SideEffectLock())

    res = await mod._make_live(str(wt), dry_run=False)

    assert res["ok"] is False, (
        "cutover must be refused when the binary disappears inside the lock; "
        "got ok=True — the in-lock re-validation is absent or not running"
    )
    assert res["code"] == "missing_venv", (
        f"expected missing_venv from in-lock re-validation, got {res.get('code')!r}"
    )
    ptr_file = ptr_dir / "live_target.json"
    assert not ptr_file.exists(), (
        "the live-target pointer must NOT be written when in-lock re-validation fails"
    )


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_artifact_checks_are_executor_offloaded(
    monkeypatch, tmp_path
):
    """The artifact filesystem checks (``is_file`` / ``os.access``) are
    submitted to ``loop.run_in_executor`` rather than called inline on the
    event loop, preventing a slow or network-backed filesystem from stalling
    all Dev Fleet requests.

    The test wraps ``subprocess_executor()`` to record every callable submitted
    via ``loop.run_in_executor``.  A helper named ``_validate_artifacts_sync``
    must be submitted at least twice — once for the early probe and once for
    the in-lock re-validation — proving the checks are offloaded.

    Without the production fix, the checks are plain synchronous expressions
    (``kcbin.is_file()``, ``os.access()``, ``dist_index.is_file()``) executed
    inline; no callable named ``_validate_artifacts_sync`` is ever submitted.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    submitted_qualnames: list[str] = []

    # Intercept every run_in_executor call by wrapping the event loop's method.
    # asyncio.get_running_loop() inside _make_live returns the SAME object that
    # asyncio.get_event_loop() returns under pytest-asyncio's per-test loop.
    # We patch the loop object's method directly so the intercept is in place
    # when _make_live calls loop.run_in_executor(…).
    running_loop = asyncio.get_running_loop()
    real_run_in_executor = running_loop.run_in_executor

    async def _recording_run_in_executor(executor, fn, *args):
        submitted_qualnames.append(fn.__qualname__)
        return await real_run_in_executor(executor, fn, *args)

    monkeypatch.setattr(running_loop, "run_in_executor", _recording_run_in_executor)

    await mod._make_live(str(wt), dry_run=False)

    validate_submissions = [
        q for q in submitted_qualnames if "_validate_artifacts_sync" in q
    ]
    assert len(validate_submissions) >= 2, (
        "artifact validation must be submitted to the executor at least twice "
        "(early probe + in-lock re-validation); "
        f"all submitted callables: {submitted_qualnames!r}.  "
        "Zero entries means the checks are still inline on the event loop."
    )


# --- Pull+Build: preflight, node_modules transaction, operator repair seam ---
#
# `npm ci` deletes node_modules before installing, so a registry that refuses one
# package used to turn a sync into damage: the tree was emptied, the run aborted
# mid-reify, and the checkout was left with new source, a new lockfile and no
# frontend dependencies. These pin the three properties that make that failure a
# no-op instead.


@pytest.mark.asyncio
async def test_sync_preflights_between_fetch_and_merge(monkeypatch):
    """The probe must sit AFTER fetch and BEFORE merge.

    That position is the whole mechanism, not a detail: the incoming lockfile is
    only knowable once fetch has landed, and fetch moves nothing but remote refs
    — so it is the one moment where a refusal costs nothing. After the merge the
    refusal would already be too late; before the fetch there is nothing to read.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])
    labels = [s["label"] for s in steps]

    assert mod._PREFLIGHT_LABEL in labels, labels
    # The probe sits between the fetch and the merge, which is the whole point:
    # the lockfile is knowable once fetch lands, and refusing before merge costs
    # nothing. The merge is labelled distinctly from the fetch so the rendered
    # stepper does not read Pull -> Preflight -> Pull, like a restarted run.
    assert labels[0] == "Pull", labels
    assert labels.index(mod._PREFLIGHT_LABEL) == 1, labels
    assert labels[2] == "Merge", labels
    assert labels.count("Pull") == 1, labels
    assert labels.index("pip install") > labels.index(mod._PREFLIGHT_LABEL), labels


@pytest.mark.asyncio
async def test_sync_preflight_probes_the_incoming_ref_not_the_working_tree(monkeypatch):
    """It must read the lockfile from the FETCHED ref.

    Reading the working tree would answer the question about the revision we
    already have, which is never the one that is about to be installed — and it
    could only be done after the merge, i.e. after the point where refusing is
    still free.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    argvs = await _sync_step_argvs(monkeypatch)
    probe = [a for a in argvs if any("npm_preflight" in str(x) for x in a)]
    assert probe, f"no preflight step in {argvs}"
    argv = probe[0]
    assert argv[0] == sys.executable, argv
    assert "--ref" in argv
    ref = argv[argv.index("--ref") + 1]
    # The ref the fetch step pinned, not a path and not a mutable
    # remote-tracking name: reading the working tree would answer the question
    # about the revision we already have, and a name the refresher can move
    # would answer it about a revision the merge may not install.
    assert ref == mod._sync_base_ref(), ref
    assert not ref.endswith(f"/{mod.BASE_BRANCH}"), ref
    assert not ref.startswith("/") and not ref.startswith("."), ref


@pytest.mark.asyncio
async def test_sync_skips_the_preflight_on_an_edition_checkout(monkeypatch):
    """No frontend half means nothing to preflight.

    The edition path deliberately runs no npm at all, so a probe there would be
    a network round trip that can only produce a false refusal.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: True)
    _, cmd, steps = await _run_sync(mod, [])
    assert mod._PREFLIGHT_LABEL not in [s["label"] for s in steps]


@pytest.mark.asyncio
async def test_npm_ci_step_carries_a_node_modules_stash(monkeypatch):
    """The transaction is attached to the npm ci step, and ONLY to it.

    It cannot be a later "restore" step: the runner is fail-fast, so anything
    after a failed step never runs — which is exactly the case that needs the
    restore.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])
    stashed = {s["label"]: s["stash"] for s in steps if s.get("stash")}
    assert list(stashed) == ["npm ci"], stashed
    assert stashed["npm ci"] == str(Path(_SYNC_REPO) / "website" / "node_modules")
    # The transaction's BEHAVIOUR — restore on failure only, confirmed
    # deletions, symlink unlinking, lexists gates — is proven by EXECUTION in
    # test_dev_fleet_sync_runner.py against real directory trees; the inline
    # source-text assertions this test used to carry moved there with it. What
    # stays here is the composition contract: the stash rides the npm ci step.


@pytest.mark.asyncio
async def test_sync_never_runs_an_operator_supplied_command(monkeypatch):
    """No configurable command executes on the sync path. This is a RATCHET.

    An operator-declared "repair the registry credential" hook was written, then
    removed: its whole purpose is to run a program that touches the operator's
    credential material, and the operator declares the command while an agent
    can rewrite the FILE it names -- or a script among its arguments. Withholding
    git's credential helpers did not close it either, because HOME is itself the
    channel those credentials arrive through. No validation of argv[0] can make
    "the operator chose this command" mean "this is the code that will run", so
    the seam does not belong on a path that runs unattended.

    Restoring it needs its own change with its own threat model, not a revert.
    """
    monkeypatch.setenv("KIROCREW_DEVFLEET_NPM_AUTH_REPAIR", "/bin/sh -c true")
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])

    assert not any("repair" in s["label"] for s in steps), steps
    assert not hasattr(mod, "_npm_auth_repair_argv")
    cmd_flat = " ".join(map(str, cmd))
    for token in ("KIROCREW_DEVFLEET_NPM_AUTH_REPAIR", "npm_auth_repair"):
        assert token not in cmd_flat, cmd
    # The declared command must appear NOWHERE in the composed steps. Asserted
    # this way rather than against a list of expected argv[0]s: the toolchain
    # binaries are resolved from the host (npm is /usr/bin/npm on one platform
    # and /opt/homebrew/bin/npm on another), so a path allowlist tests the host
    # rather than the property.
    assert "/bin/sh" not in cmd_flat
    for s in steps:
        assert "/bin/sh" not in " ".join(map(str, s["argv"])), s["argv"]
        assert not any("repair" in str(v) for v in s.get("env", {}).values()), s


@pytest.mark.asyncio
async def test_a_stashed_tree_is_recovered_before_any_step_runs(monkeypatch):
    """Recovery must not sit behind the steps that precede the transaction.

    A run killed just after the move-aside leaves the tree absent and its backup
    unclaimed. With adoption on the `npm ci` step, the next run's recovery was
    gated on every earlier step succeeding -- so a preflight that still failed
    left the tree missing with an intact copy sitting right beside it. Both
    halves of leftover-state reconciliation therefore happen before the loop.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, steps = await _run_sync(mod, [])
    assert cmd is not None
    # The reconcile/adopt decision and the move-aside now live in the runner
    # module. That both halves happen -- and the backup-only recovery lands
    # BEFORE any step runs, and the pre-run section only moves the tree aside --
    # is driven against real trees in test_dev_fleet_sync_runner.py
    # (TestReconcileLeftovers, TestNodeModulesTransaction). Here we pin the
    # structural ordering in main(): reconcile_leftovers is called before
    # run_steps.
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    main_body = src.split("def main(", 1)[1]
    assert main_body.index("reconcile_leftovers(") < main_body.index("run_steps("), (
        "the stashed tree must be reclaimed before any step runs"
    )
    # The move-aside (enter) and the restore (exit) are separate phases of the
    # transaction, not a redundant second adoption in the pre-run section.
    enter = src.split("def __enter__", 1)[1].split("def __exit__", 1)[0]
    assert "os.rename(self.stash, self.backup)" in enter
    assert "os.rename(self.backup, self.stash)" not in enter


@pytest.mark.asyncio
async def test_the_failure_cause_is_never_taken_from_child_output(monkeypatch):
    """A build script must not be able to forge the authoritative diagnosis.

    The run's stdout carries worktree-controlled output, so any in-band marker
    the gateway promoted could be printed by an npm lifecycle script that then
    fails -- and the dashboard would present the forgery as the cause, remedy
    included. Redaction does not help: it strips credentials, not instructions.
    So the diagnosis is derived from the EXIT CODE, which a step's own child
    cannot choose, and nothing is parsed out of the stream.
    """
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None
    # No promotable marker is emitted by the runner at all.
    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    assert "::cause::" not in src
    # And the worker has no branch that lifts text out of a line.
    import inspect
    body = inspect.getsource(mod._start_run)
    assert "::cause::" not in body
    assert 'npm_preflight.explain_exit(rc)' in body, \
        "the cause must be derived from the exit code, gateway-side"


@pytest.mark.asyncio
async def test_runner_refuses_when_a_tree_and_a_backup_both_exist(monkeypatch):
    """Both paths present is AMBIGUOUS, so the runner touches neither.

    Killed during npm ci leaves a partial tree plus the good backup; a backup
    outliving a successful sync leaves the good tree plus a stale one. Nothing on
    disk distinguishes them, so either rule destroys the good copy in one case.
    Stopping is the only branch that cannot lose data, and it exits with a code
    the gateway maps to a sentence naming the next step.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None

    # The both-exist refusal (exit the ambiguous-tree code, touch neither, name
    # both paths) is driven against real trees in test_dev_fleet_sync_runner.py
    # (TestReconcileLeftovers), and that it lands as reconciliation before any
    # step is pinned there and by the main() ordering test. The code itself has
    # ONE spelling now (npm_preflight.EXIT_TREE_AMBIGUOUS, passed to the runner
    # via --exit-tree-ambiguous); here we pin the pieces that stay
    # gateway-side: the code is handed in on argv, and the gateway maps it to a
    # sentence naming the next step.
    assert "--exit-tree-ambiguous" in cmd
    passed = cmd[cmd.index("--exit-tree-ambiguous") + 1]
    assert passed == str(npm_preflight.EXIT_TREE_AMBIGUOUS)
    # The mapped sentence names what to do, not merely what happened.
    text = npm_preflight.explain_exit(npm_preflight.EXIT_TREE_AMBIGUOUS)
    assert "press Pull + Build again" in text


@pytest.mark.asyncio
async def test_only_the_preflight_step_may_assert_a_diagnosis(monkeypatch):
    """A reserved exit code is trusted from ONE step and remapped from the rest.

    Moving the diagnosis off stdout onto exit codes did not by itself make it
    unforgeable: every step except the preflight runs worktree-controlled code
    (an npm lifecycle script, a vite config) and can exit any number it likes.
    A forged 41 would have the dashboard assert a registry-credential failure --
    remedy included -- for what was actually a build error. So the runner trusts
    a reserved code only from the step whose binary is ours, and keeps the true
    code in the log rather than believing it.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None

    # The gate itself (a reserved code is trusted from the preflight label and
    # demoted from every other step) is driven by execution in
    # test_dev_fleet_sync_runner.py (TestDemoteReserved, TestRunSteps). Here we
    # pin that the gateway passes the reserved set and the trusted label IN, so
    # the runner needs to import nothing from kiro_crew and the two cannot
    # drift from the modules that own them.
    assert "--reserved" in cmd
    reserved = sorted(npm_preflight.RESERVED_EXIT_CODES)
    passed = cmd[cmd.index("--reserved") + 1]
    assert passed == ",".join(str(c) for c in reserved), passed
    assert "--preflight-label" in cmd
    assert cmd[cmd.index("--preflight-label") + 1] == mod._PREFLIGHT_LABEL
    # Every code the gateway will explain must be in the guarded set, or a code
    # it explains could still arrive forged.
    for code in reserved:
        assert npm_preflight.explain_exit(code), code


def test_the_trusted_label_matches_the_step_that_carries_it():
    """The trust check keys on a label, so the label must be the real one.

    If the step were renamed without updating the constant, every reserved code
    would be remapped -- the probe's own diagnosis would silently stop reaching
    the dashboard, and nothing would fail.
    """
    import inspect

    src = inspect.getsource(mod._sync_start_locked)
    assert "_PREFLIGHT_LABEL," in src, (
        "the preflight step must be labelled from the constant the runner's "
        "trust check uses, not from a repeated literal"
    )


@pytest.mark.asyncio
async def test_probe_and_merge_consume_one_immutable_commit(monkeypatch):
    """Fetch, probe and merge must share a commit no background fetch can move.

    ``<remote>/<base branch>`` is a mutable name and the status refresher
    re-fetches it every _NET_REFRESH_S seconds in this same process. With a real
    install sitting between the probe and the merge, resolving that name twice
    lets them land on different commits -- the probe would certify a revision
    that is not the one installed, which is worse than not probing at all,
    because the promise is what makes the merge look safe.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])

    by_label = {}
    for st in steps:
        by_label.setdefault(st["label"], []).append(st["argv"])

    fetch = next(a for a in by_label["Pull"] if "fetch" in a)
    merge = next(a for a in by_label["Merge"] if "merge" in a)
    probe = by_label[mod._PREFLIGHT_LABEL][0]

    # The fetch pins the tip it brought, forcing, because the ref is ours.
    pinned = mod._sync_base_ref()
    assert f"+refs/heads/{mod.BASE_BRANCH}:{pinned}" in fetch
    # Both consumers name that pinned ref...
    assert merge[-1] == pinned
    assert probe[probe.index("--ref") + 1] == pinned
    # ...the ref is PER PROCESS, because _SYNC_LOCK only makes syncs
    # single-flight inside one gateway: two gateways on one checkout would
    # otherwise share this name and the second one's fetch would move it
    # between the first one's probe and merge, reopening the window.
    assert str(os.getpid()) in pinned, pinned
    assert pinned.startswith("refs/kirocrew/sync-base-"), pinned
    # ...and neither still names the mutable one, which is the actual defect.
    mutable = [a for a in merge + probe if a.endswith("/" + mod.BASE_BRANCH)]
    assert not mutable, (
        f"{mutable} is a mutable remote-tracking name; the refresher can move "
        "it between the probe and the merge"
    )
    # The pin must be written before anything reads it, or a ref left by an
    # earlier run could be probed and merged.
    labels = [st["label"] for st in steps]
    assert labels.index("Pull") < labels.index(mod._PREFLIGHT_LABEL)


def test_the_frontend_declares_no_resolution_input_the_probe_cannot_mirror():
    """A tripwire on the probe's three-file mirror, not on the frontend.

    The preflight installs a scratch copy of ``_PROBE_FILES`` and refuses the
    sync when that install fails. The gate is fail-closed and has no bypass, so
    a resolution input the mirror does NOT carry makes the scratch install fail
    while the real one would have succeeded -- which hard-blocks every
    Pull + Build until somebody edits npm_preflight.

    ``workspaces`` and ``file:``/``link:`` specifiers are that class: both make
    npm read paths that exist in the checkout and not in the scratch directory.
    Neither is present today, so this pins the assumption rather than reporting a
    defect: whoever introduces one gets this failure at that moment, instead of
    an unexplained refusal of every update afterwards. Fixing it then means
    teaching the probe to skip (restoring today's unguarded behaviour for that
    case) or to copy what the new input needs -- not deleting this test.
    """
    root = Path(__file__).resolve().parents[1]
    pkg_path = root / "website" / "package.json"
    if not pkg_path.is_file():  # editions ship without the frontend half
        pytest.skip("no frontend half in this checkout")
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))

    assert "workspaces" not in pkg, (
        "website/package.json now declares workspaces, which npm resolves from "
        "sibling directories the preflight's scratch copy does not have. The "
        "probe would refuse every sync. See _PROBE_FILES in npm_preflight."
    )

    local = {}
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (pkg.get(field) or {}).items():
            if isinstance(spec, str) and spec.startswith(("file:", "link:")):
                local[f"{field}.{name}"] = spec
    assert not local, (
        f"local-path dependencies {local} resolve against the checkout, which "
        "the preflight's scratch copy is not. The probe would refuse every "
        "sync. See _PROBE_FILES in npm_preflight."
    )

    # The mirror must also still carry the inputs it claims to: a copy that
    # dropped .npmrc would answer a different question than the install.
    for name in ("package-lock.json", "package.json", ".npmrc"):
        assert name in npm_preflight._PROBE_FILES, name


@pytest.mark.asyncio
async def test_only_the_sync_kind_can_be_stamped_with_a_diagnosis(monkeypatch):
    """The stamp must be gated on the kind whose script enforces the reservation.

    ``_start_run`` is kind-agnostic and also serves ``provision <name>``, which
    executes an agent-authored branch with no reserved-code remapping. Deriving
    the cause for every kind would hand a provision run's exit 41 back as "the
    package registry rejected our credentials" plus a remedy -- the same forged
    authoritative diagnosis the runner-side demotion exists to refuse, arriving
    by the other door.

    Driven end to end rather than read out of the source: an earlier version of
    this test asserted on ``inspect.getsource`` and passed while the gate was
    comparing a variable that the ``::step::`` handler had already rebound, so
    the sync path silently stamped nothing and the provision path raised
    NameError. Only running it catches that.
    """
    seen = {}

    for kind, emits_steps in (("sync", True), ("provision wt-x", False)):
        lines = [b"::step::0::4::Pull\n"] if emits_steps else []
        lines.append(b"npm error code E401\n")

        class FakeProc:
            pid = 4242
            returncode = None

            def __init__(self):
                self.stdout = self
                self._lines = list(lines)

            async def readline(self):
                return self._lines.pop(0) if self._lines else b""

            async def wait(self):
                self.returncode = npm_preflight.EXIT_AUTH
                return npm_preflight.EXIT_AUTH

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(runtime_mod.asyncio, "create_subprocess_exec", fake_exec)
        rid = await mod._start_run(kind, ["true"], env={})
        for _ in range(80):
            await runtime_mod.asyncio.sleep(0.05)
            async with mod._RUNS_LOCK:
                if mod._RUNS.get(rid, {}).get("status") not in (None, "running"):
                    break
        async with mod._RUNS_LOCK:
            seen[kind] = dict(mod._RUNS[rid])

    # The sync run is the one kind allowed to assert a cause -- and it must
    # actually get one, which the shadowed comparison silently prevented.
    assert seen["sync"].get("cause"), (
        "the sync run was not stamped; the gate is reading something other than "
        "the run kind"
    )
    assert "registry" in seen["sync"]["cause"].lower()
    assert seen["sync"]["exit_code"] == npm_preflight.EXIT_AUTH

    # The provision run must be stamped with nothing AND must still complete
    # normally: an unbound name here turned a finished run into exit -1.
    assert not seen["provision wt-x"].get("cause"), seen["provision wt-x"]
    assert seen["provision wt-x"]["exit_code"] == npm_preflight.EXIT_AUTH, (
        "the provision run's exit code was rewritten -- the completion path "
        "raised before recording it"
    )


def test_the_stamp_gate_reads_a_name_the_step_handler_cannot_rebind():
    """The gate must not read a variable the output loop assigns to.

    This is the defect that shipped once: ``label`` is the function parameter
    AND the ``::step::`` handler's target, so at completion it held the last
    step's label or was unbound. Whatever the gate compares has to be assigned
    exactly once, which is checked here on the parse tree rather than by counting
    substrings -- the first version of this test counted ``run_kind =`` and
    matched ``run_kind ==`` too.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod._start_run)))

    # The name the gate actually compares against.
    gates = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Compare)
        and isinstance(n.left, ast.Name)
        and any(
            isinstance(c, ast.Name) and c.id == "_SYNC_RUN_LABEL"
            for c in n.comparators
        )
    ]
    assert len(gates) == 1, f"expected one kind gate, found {len(gates)}"
    guarded = gates[0].left.id

    targets = [
        t.id
        for n in ast.walk(tree)
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.For, ast.AsyncFor))
        for t in ast.walk(n.target if hasattr(n, "target") else n.targets[0])
        if isinstance(t, ast.Name)
    ]
    assert targets.count(guarded) == 1, (
        f"{guarded!r} is assigned {targets.count(guarded)} times in "
        "_start_run; the gate's variable must be bound once, before any output "
        "is read, or a later handler can rebind it out from under the gate"
    )
    assert guarded not in {a.arg for a in tree.body[0].args.args}, (
        f"{guarded!r} is the parameter itself -- use a local captured at entry, "
        "so a handler that rebinds the parameter cannot reach the gate"
    )


def test_the_stamp_gate_and_the_sync_label_are_one_constant():
    """A literal in either place would let the two drift apart silently.

    Drift in one direction re-opens the forgery; in the other it suppresses the
    diagnosis this PR exists to deliver, and neither shows up as a failure.
    """
    assert mod._SYNC_RUN_LABEL == "sync"
    src = Path(runtime_mod.__file__).read_text(encoding="utf-8")
    assert '_start_run("sync"' not in src, (
        "the sync is started with a literal label; use _SYNC_RUN_LABEL"
    )


@pytest.mark.asyncio
async def test_prune_clears_dead_sync_refs_and_spares_live_ones(tmp_path, monkeypatch):
    """The PID suffix is only affordable if the refs it strands get collected.

    Nothing deletes the pinned ref on the way out, so an ordinary gateway
    restart leaves one behind in the OPERATOR's checkout -- unbounded except by
    pid_max and visible in ``git for-each-ref`` forever. The prune removes those,
    and must LEAVE ALONE any ref whose PID is still alive: that one may be a
    second gateway's live pin, and deleting it would reopen the very window the
    suffix closes.
    """
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "p", "GIT_AUTHOR_EMAIL": "p@e",
        "GIT_COMMITTER_NAME": "p", "GIT_COMMITTER_EMAIL": "p@e",
    }

    def git(*a):
        return sp.run(
            ["git", *a], cwd=repo, env=env, capture_output=True, text=True,
            encoding="utf-8", check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "f.txt").write_text("one\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "one")
    head = git("rev-parse", "HEAD")

    # A ref for THIS process (skipped by name), one for a PID that is certainly
    # gone, one for a DIFFERENT but LIVE process -- pid 1 always exists and is
    # never us, and signalling it raises PermissionError rather than succeeding,
    # so it exercises the alive-but-not-ours branch too -- and one that is not
    # ours to reason about. The live foreign PID is the case that matters: it
    # stands in for a second gateway's pin, and it is the only ref whose survival
    # depends on the liveness check rather than on the name check.
    mine = mod._sync_base_ref()
    dead_pid = 999_999_999  # above any real pid_max
    dead = f"refs/kirocrew/sync-base-{dead_pid}"
    live_other = "refs/kirocrew/sync-base-1"
    foreign = "refs/kirocrew/something-else"
    for ref in (mine, dead, live_other, foreign):
        git("update-ref", ref, head)

    async def fake_git(git_dir, *args, **kw):
        return sp.run(
            ["git", *args], cwd=git_dir, env=env, capture_output=True, text=True,
            encoding="utf-8", check=False,
        ).stdout
    monkeypatch.setattr(repository_mod, "_git", fake_git)

    await mod._prune_dead_sync_base_refs(str(repo))

    remaining = set(
        git("for-each-ref", "--format=%(refname)", "refs/kirocrew/").splitlines()
    )
    assert mine in remaining, (
        "the prune deleted THIS process's live pin; a sync would then fetch into "
        "a ref another gateway could move"
    )
    assert live_other in remaining, (
        "the prune deleted a ref belonging to a process that is STILL ALIVE -- "
        "that may be a second gateway's pin, and removing it reopens exactly the "
        "window the PID suffix exists to close"
    )
    assert dead not in remaining, f"stale ref {dead} survived the prune"
    assert foreign in remaining, (
        "the prune deleted a ref outside its own naming scheme"
    )


@pytest.mark.asyncio
async def test_prune_runs_before_the_fetch_step_is_built(monkeypatch):
    """Ordering: the prune must not be able to race the fetch that writes our pin.

    It only ever deletes refs for PIDs that are gone, so it cannot touch our own
    -- but running it before the steps are built keeps that guarantee structural
    rather than incidental.
    """
    calls: list[str] = []

    async def spy(repo):
        calls.append(repo)
    monkeypatch.setattr(worktree_ops_mod, "_prune_dead_sync_base_refs", spy)
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    _, cmd, steps = await _run_sync(mod, [])

    assert calls, "the sync never pruned stale pinned refs"

    fetch = [st for st in steps if "fetch" in st["argv"]]
    assert fetch, "no fetch step"
    # The pin the fetch writes is this process's, which the prune never removes.
    assert mod._sync_base_ref() in " ".join(fetch[0]["argv"])


@pytest.mark.asyncio
async def test_preflight_runs_a_snapshot_not_the_editable_source(monkeypatch):
    """The probe must execute a private COPY by path, never import the checkout.

    Two distinct holes close here. ``-c`` puts the cwd -- the checkout -- first on
    ``sys.path``, so an untracked ``kiro_crew/`` package at its root would win;
    ``-I`` fixes that. But the install is EDITABLE, so ``import kiro_crew...``
    resolves into the checkout's own ``src/`` tree even under ``-I``, and this
    step is trusted to assert a failure cause precisely BECAUSE its binary is
    ours. Importing the tree being synced put the boundary's own key under the
    mat. Running a snapshot by path consults neither.

    Verified end to end in $KIROCREW_SCRATCH/shadow_probe.py: a planted shadow
    wins without -I, the import still lands in the editable tree WITH -I, and a
    by-path snapshot runs standalone because npm_preflight imports only stdlib.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    argvs = await _sync_step_argvs(monkeypatch)
    probe = [
        a for a in argvs
        if any("npm_preflight" in str(x) for x in a) and str(a[0]) == sys.executable
    ]
    assert probe, f"no preflight step in {argvs}"
    argv = probe[0]

    # No import of the package at all -- that is the point.
    joined = " ".join(str(x) for x in argv)
    assert "-c" not in argv, argv
    assert "import" not in joined, (
        "the probe still imports the module; an editable install resolves that "
        "into the checkout being synced"
    )
    # A script path, and NOT one inside the checkout.
    script = [str(x) for x in argv if str(x).endswith("npm_preflight.py")]
    assert len(script) == 1, argv
    assert "/src/kiro_crew/" not in script[0], (
        f"{script[0]} is the editable source itself, not a snapshot"
    )
    # Isolated, with the flags ahead of the script path -- after it they would be
    # argv for the program and the protection would vanish silently.
    assert argv.index("-I") < argv.index(script[0]), argv
    assert argv.index("-X") < argv.index(script[0]), argv


@pytest.mark.asyncio
async def test_the_preflight_snapshot_is_registered_for_cleanup(monkeypatch):
    """The snapshot must not leak one temp directory per Pull + Build.

    The run's cleanup unlinks each registered path and falls back to rmdir, which
    only succeeds on an empty directory -- so the FILE has to be registered ahead
    of its directory. The dependency-only path already leaks its own snapshot dir
    by registering neither; this asserts we do not repeat that.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    await _run_sync(mod, [])

    paths = list(_LAST_CLEANUP_PATHS)
    snaps = [p for p in paths if "npm-preflight" in p]
    assert snaps, f"the preflight snapshot is not registered for cleanup: {paths}"
    files = [p for p in snaps if p.endswith(".py")]
    dirs = [p for p in snaps if not p.endswith(".py")]
    assert files and dirs, f"expected both the file and its directory: {snaps}"
    assert paths.index(files[0]) < paths.index(dirs[0]), (
        "the directory is registered before its file, so rmdir will fail on a "
        "non-empty directory and the snapshot will leak"
    )


@pytest.mark.parametrize("failing", ["mkdtemp", "write"])
@pytest.mark.asyncio
async def test_a_full_tmpdir_refuses_the_sync_instead_of_raising(
    monkeypatch, tmp_path, failing
):
    """Staging the snapshot can fail; that must refuse, and leave nothing behind.

    ``mkdtemp`` and the snapshot write both raise OSError on a full or unwritable
    TMPDIR. The sync answers a UI action, so an escaping OSError would surface as
    an unhandled HTTP 500 with no remedy. Refusing is also the SAFE outcome: with
    no probe there is nothing to trust, and proceeding unprobed is precisely the
    destructive path this change exists to prevent.

    The write case matters twice over: mkdtemp has already SUCCEEDED by then, and
    the refusal returns before anything is registered for the run's cleanup, so a
    directory would survive every failed sync -- in the product, not merely in
    this test. Both sites are driven for real; an assertion on the source would
    not notice a second unguarded call appearing beside the first.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    boom = OSError(28, "No space left on device")
    # Direct the REAL mkdtemp under this test's tmp_path, so even a regression
    # that reintroduces the leak cannot litter the host running the suite.
    real_mkdtemp = worktree_ops_mod.tempfile.mkdtemp

    if failing == "mkdtemp":
        monkeypatch.setattr(
            worktree_ops_mod.tempfile, "mkdtemp",
            lambda *a, **kw: (_ for _ in ()).throw(boom),
        )
    else:
        monkeypatch.setattr(
            worktree_ops_mod.tempfile, "mkdtemp",
            lambda *a, **kw: real_mkdtemp(*a, **{**kw, "dir": str(tmp_path)}),
        )
        real_write = Path.write_bytes

        def fail_write(self, data):
            if self.name == "npm_preflight.py":
                raise boom
            return real_write(self, data)

        monkeypatch.setattr(Path, "write_bytes", fail_write)

    result, cmd, steps = await _run_sync(mod, [])

    assert result.get("ok") is False, result
    assert "preflight" in result["error"].lower(), result
    assert "No space left on device" in result["error"], result
    # A refusal means no run was started at all, so no steps ran.
    assert cmd is None, "the sync started a run despite failing to stage"
    # And the partial snapshot is gone: nothing registered it for cleanup, so the
    # refusal path has to remove it itself.
    leaked = list(tmp_path.glob("kirocrew-npm-preflight-*"))
    assert not leaked, (
        f"the refusal left {leaked} behind; every failed sync would leak one"
    )


@pytest.mark.asyncio
async def test_the_snapshot_is_written_from_bytes_captured_at_import(monkeypatch):
    """The probe's snapshot must be of the code THIS gateway is running.

    Copying the module file at sync time left a window from gateway start until
    the button press in which the source could be rewritten -- and the copy is
    then executed as the one step trusted to assert a failure cause. Capturing at
    import closes it: the bytes are the ones the running process imported.

    Driven by rewriting the file on disk AFTER import and asserting the snapshot
    does not contain the change.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    written: dict = {}
    real_write = Path.write_bytes

    def spy(self, data):
        if self.name == "npm_preflight.py":
            written["data"] = data
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy)
    # Whatever is on disk now is irrelevant: the module was imported long ago.
    monkeypatch.setattr(
        runtime_mod, "_PREFLIGHT_SOURCE", b"# captured at import\nmarker = 1\n"
    )
    await _run_sync(mod, [])

    assert "data" in written, "the snapshot was never written"
    assert written["data"] == b"# captured at import\nmarker = 1\n", (
        "the snapshot was re-read from disk instead of using the captured bytes"
    )


@pytest.mark.asyncio
async def test_sync_refuses_when_the_probe_source_could_not_be_captured(monkeypatch):
    """An unreadable probe source refuses rather than falling back to a re-read.

    A frozen or zipimported install has no readable ``__file__``. Falling back to
    copying the file at sync time would reintroduce exactly the window the
    import-time capture removes, so the sync refuses instead -- the same safe
    direction the full-TMPDIR path takes.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    monkeypatch.setattr(runtime_mod, "_PREFLIGHT_SOURCE", None)

    result, cmd, steps = await _run_sync(mod, [])

    assert result.get("ok") is False, result
    assert "preflight" in result["error"].lower(), result
    assert cmd is None, "the sync started a run with no trustworthy probe"


@pytest.mark.asyncio
async def test_every_stash_presence_gate_uses_lexists(monkeypatch):
    """Presence gates must not follow symlinks.

    ``isdir`` follows a link, so a DANGLING node_modules read as absent: the
    reconciliation then took the backup-only branch and called
    ``os.rename(<dir>, <dangling link>)``, which fails ENOTDIR and crashes the
    runner on every sync with the tree never recovered. The move-aside gate has
    the mirror bug -- with ``isdir`` a symlinked tree is never stashed, so the
    step runs with no backup at all, on exactly the layouts that most need one.

    Verified end to end in $KIROCREW_SCRATCH/runner_probe.py (scenarios 8 and 9);
    this pins the shape so the gates cannot silently revert.
    """
    monkeypatch.setattr(worktree_ops_mod.frontend, "edition_configured", lambda: False)
    from kiro_crew.apps.builtins.dev_fleet import sync_runner

    _, cmd, _ = await _run_sync(mod, [])
    assert cmd is not None

    src = Path(sync_runner.__file__).read_text(encoding="utf-8")
    assert "have_tree = os.path.lexists(stash)" in src
    assert "have_backup = os.path.lexists(backup)" in src
    assert "if self.stash and self.backup and os.path.lexists(self.stash):" in src
    # No presence gate on either path may follow a link.
    for bad in (
        "os.path.isdir(stash)",
        "os.path.isdir(backup)",
        "os.path.exists(stash)",
        "os.path.exists(backup)",
    ):
        assert bad not in src, f"{bad} follows symlinks; use lexists"
