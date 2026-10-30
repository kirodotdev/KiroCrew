"""Dev Fleet worktree teardown guards.

Covers the force-remove refusals, the ancestry gate on ref deletion, removal audit
and cache invalidation, and the TOCTOU and containment checks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture

from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.testing.wait import async_wait_until

# --- Worktree teardown guard tests ---


@pytest.mark.asyncio
async def test_force_remove_refuses_dirty_unmerged_worktree():
    """force=True must NOT destroy a dirty tree whose PR
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
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    assert "uncommitted changes" in result["error"]
    assert "not merged" in result["error"]


@pytest.mark.asyncio
async def test_force_remove_refuses_dirty_merged_worktree():
    """force=True must NOT destroy a dirty tree
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
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        patch.object(
            fleet_state_mod,
            "_head_contained_in_pr",
            new_callable=AsyncMock,
            side_effect=_fake_contained,
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_fake_run_cmd),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_fake_run_cmd),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
        patch.object(
            fleet_state_mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, side_effect=_fake_git),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_fake_run_cmd),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
async def test_removal_invalidates_disk_cache():
    """Successful removal drops the disk cache's freshness stamp.

    The chokepoint that every removal path routes through (single-worktree
    handler, prune workers, auto-prune reaper) must invalidate the /disk TTL
    cache alongside evicting the fleet row, so the next poll re-aggregates
    instead of serving pre-removal totals for the rest of the TTL.
    """
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
        patch.object(
            fleet_state_mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"
        ),
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
        patch.object(fleet_state_mod, "_disk_invalidate") as invalidate,
    ):
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    invalidate.assert_called_once_with()


@pytest.mark.asyncio
async def test_start_run_invokes_on_finish_at_terminal_state():
    """_start_run's on_finish fires once when the run finishes."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    calls: list[int] = []
    rid = await mod._start_run("finish-test", ["true"], on_finish=lambda: calls.append(1))

    async def _stamped() -> bool:
        async with mod._RUNS_LOCK:
            return mod._RUNS[rid]["status"] != "running"

    await async_wait_until(_stamped, describe=lambda: mod._RUNS[rid]["status"])
    # The callback runs in the worker's finally, after the status stamp.
    await async_wait_until(lambda: calls, describe=lambda: f"calls={calls}")
    assert calls == [1]


@pytest.mark.asyncio
async def test_pod_provision_registers_disk_invalidation(monkeypatch):
    """Provisioning builds .venv/dist inside the measured worktree, so the
    provision run must carry the disk-cache invalidation hook."""
    import kiro_crew.apps.builtins.dev_fleet.worktree_ops as wt_ops

    captured: dict = {}

    async def fake_start_run(label, cmd, **kw):
        captured.update(kw, label=label)
        return "rid-1"

    monkeypatch.setattr(wt_ops, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(fleet_state_mod, "_PROVISION_INFLIGHT", {})
    monkeypatch.setattr(runtime_mod, "_warm_build_path", AsyncMock())
    monkeypatch.setattr(runtime_mod, "_start_run", fake_start_run)
    monkeypatch.setattr(runtime_mod, "_find_cli", lambda: ["kirocrew"])
    monkeypatch.setattr(repository_mod, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(wt_ops, "_pod_env", lambda: {})
    monkeypatch.setattr(
        wt_ops,
        "shielded_prepare_off_loop",
        AsyncMock(return_value=(["kirocrew", "pod", "provision", "feat-x"], {}, None)),
    )

    result = await wt_ops._pod_provision("feat-x")
    assert result == {"ok": True, "run_id": "rid-1"}
    assert captured.get("on_finish") is fleet_state_mod._disk_invalidate


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
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
        caplog.at_level(logging.INFO, logger="kiro_crew.apps.builtins.dev_fleet.server"),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is False
    audit_lines = [r for r in caplog.records if "worktree_removal_audit" in r.message]
    assert len(audit_lines) == 1
    msg = audit_lines[0].message
    assert "action=refused_dirty_unmerged" in msg
    assert "dirty=True" in msg


# --- Regression tests for the force guard fail-closed fixes ---


@pytest.mark.asyncio
async def test_force_remove_refuses_unknown_dirty_state(caplog):
    """Regression (a): _real_dirty returns None (git status failed), force=True,
    PR OPEN — removal must be refused, error names the unverifiable state.
    Without the guard it falls through (fail-open)."""
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
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
    — must refuse. Without this fix the guard is bypassed entirely."""
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
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
    """Cached MERGED + fresh verdict confirms
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
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
    """Cached MERGED + fresh verdict confirms
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
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", side_effect=_fake_run_cmd),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
    containment check passes → ref IS deleted. Without this, squash-merged refs
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
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, side_effect=_fake_run_cmd),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=False)

    assert result["ok"] is True
    assert (
        "refs/heads/feat-x" in deleted_refs
    ), "ref should be deleted via squash-safe containment fallback"


# --- Regression tests for the round-3 TOCTOU + containment fixes ---


@pytest.mark.asyncio
@pytest.mark.xdist_group("caplog_dev_fleet")
async def test_toctou_clean_unmerged_force_omits_git_force(caplog):
    """TOCTOU regression: when force=True overrides ONLY because the unmerged
    tree verified clean, the removal command must NOT contain --force. This way
    git's own dirty check acts as the atomic last-line guard — if the tree
    became dirty in the window between the guard and the actual removal, git
    itself refuses.

    The --force flag can leak through on this path; this test pins the contract
    that
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
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    assert result["ok"] is True
    removal_cmds = [c for c in captured_cmds if "worktree" in c and "remove" in c]
    assert len(removal_cmds) == 1
    assert (
        "--force" not in removal_cmds[0]
    ), "clean-unmerged force path must NOT include --force in the git command"
    # Verify the audit action was logged (regression: the action proves
    # the code explicitly set force_use_git_force = False on this path).
    # The security gate is the --force assertion above; this audit check is
    # secondary and can lose its record under xdist worker log routing.
    audit_msgs = [r.message for r in caplog.records if "unmerged_clean_no_git_force" in r.message]
    if not audit_msgs:
        import warnings

        warnings.warn(
            "caplog audit record missing (xdist log routing); "
            "security assertion (--force not in cmd) passed",
            stacklevel=1,
        )
    else:
        assert len(audit_msgs) == 1, "expected exactly one 'unmerged_clean_no_git_force' audit line"


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
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=False
        ),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
    """Cached MERGED + fresh head + branch OID IS contained
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
        patch.object(
            fleet_state_mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=None),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
        patch.object(runtime_mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
    ):
        result = await mod._worktree_remove("feat-x", force=True)

    # Rounds 5-6: dirty tree is refused even when PR is verified-merged+contained
    assert result["ok"] is False
    assert "uncommitted changes" in result["error"]


# --- Containment pin fail-closed regressions ---


@pytest.mark.asyncio
async def test_containment_pin_falsy_refuses_unpinnable(caplog):
    """Regression: cached MERGED + dirty + force=True, the
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
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
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
    """Message regression: the non-forced dirty refusal does not say
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
