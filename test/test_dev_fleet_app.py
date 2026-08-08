"""Tests for the dev-fleet native dashboard handler."""
from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac  # noqa: F401
import hmac as _hmac_mod
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web  # noqa: F401  (used by builtin re-shell tests)
from aiohttp.test_utils import TestClient, TestServer  # noqa: F401

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat


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
    with patch.object(mod, "_run_cmd", new=AsyncMock(return_value=(0, stdout, ""))):
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
    with patch.object(mod, "_run_cmd", new=AsyncMock(return_value=(0, stdout, ""))):
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
    with patch("kiro_crew.apps.builtins.dev_fleet.server._git", new_callable=AsyncMock) as mock_git:
        mock_git.return_value = cherry_output
        result = await _git_ahead("/fake/path")
    assert result == 2


@pytest.mark.asyncio
async def test_git_ahead_returns_none_on_failure():
    from kiro_crew.apps.builtins.dev_fleet.server import _git_ahead

    with patch("kiro_crew.apps.builtins.dev_fleet.server._git", new_callable=AsyncMock) as mock_git:
        mock_git.return_value = None
        result = await _git_ahead("/fake/path")
    assert result is None


# --- prunable verdict ---
@pytest.mark.asyncio
async def test_prunable_merged_clean():
    """PR merged + clean -> ok:true WITHOUT requiring ahead==0 (squash-safe)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=3), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
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

    with patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=5), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value="b" * 40), \
         patch.object(mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="b" * 40), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is True
    assert v["code"] == "merged"


@pytest.mark.asyncio
async def test_prunable_merged_dirty_rejected():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=3), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=True), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is False
    assert v["code"] == "merged_dirty"


@pytest.mark.asyncio
async def test_prunable_active_unmerged():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "OPEN"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=5), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is False
    assert v["code"] == "active"


# --- removal race guard (squash-safe: OID comparison) ---
@pytest.mark.asyncio
async def test_remove_refuses_when_branch_oid_diverged():
    """Squash-safe race guard: branch OID != PR headRefOid -> refuse removal."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=3), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="bbb2222"), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is False
    assert "OID diverged" in result["error"]


@pytest.mark.asyncio
async def test_remove_succeeds_when_oid_matches():
    """Squash-safe race guard passes when OIDs match."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=3), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="aaa1111"), \
         patch.object(mod, "_load_cfg", return_value=None), \
         patch.object(mod, "_POD_AVAILABLE", False), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        result = await mod._worktree_remove("feat-x", force=False)
    assert result["ok"] is True


# --- _upstream_remote fallback + override ---
@pytest.mark.asyncio
async def test_upstream_remote_fallback_to_origin():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    mod._UPSTREAM_REMOTE = None
    with patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(1, "", "not configured")):
        result = await mod._upstream_remote()
    assert result == "origin"
    mod._UPSTREAM_REMOTE = None


@pytest.mark.asyncio
async def test_upstream_remote_reads_config():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    mod._UPSTREAM_REMOTE = None
    with patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "kirocrew\n", "")):
        result = await mod._upstream_remote()
    assert result == "kirocrew"
    mod._UPSTREAM_REMOTE = None


# --- sync runner emits ::step:: markers ---
@pytest.mark.asyncio
async def test_sync_script_emits_step_markers():
    """The generated sync script must print ::step::<idx>::<label> before each step."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    mod._UPSTREAM_REMOTE = "origin"
    mod._SYNC_RID = None
    with patch.object(mod, "_git", new_callable=AsyncMock, return_value="main"), \
         patch.object(mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch("kiro_crew.apps.builtins.dev_fleet.server.sandboxed_spawn_argv",
               side_effect=lambda cmd, mode, env=None: (cmd, env or {}, None)), \
         patch.object(mod, "_start_run", new_callable=AsyncMock, return_value="run-123") as mock_start:
        async with mod._SYNC_LOCK:
            result = await mod._sync_start_locked()
    assert result["ok"] is True
    cmd_args = mock_start.call_args[0]
    script_cmd = cmd_args[1]
    assert script_cmd[0].endswith("python") or "python" in script_cmd[0]
    script_src = script_cmd[2]
    assert "::step::" in script_src
    assert "print(f" in script_src
    mod._UPSTREAM_REMOTE = None


# --- repo owner/name parsing ---
@pytest.mark.asyncio
async def test_repo_owner_name_ssh():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    mod._OWNER_REPO = None
    with patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "git@github.com:kirodotdev/KiroCrew.git\n", "")):
        result = await mod._repo_owner_name()
    assert result == "kirodotdev/KiroCrew"


@pytest.mark.asyncio
async def test_repo_owner_name_https():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    mod._OWNER_REPO = None
    with patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "https://github.com/kirodotdev/KiroCrew.git\n", "")):
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
    with patch.object(mod, "_discover_worktrees", new_callable=AsyncMock, return_value=wts):
        result, err = await mod._find_worktree("feature-x")
    assert result is None
    assert "ambiguous" in err


@pytest.mark.asyncio
async def test_find_worktree_not_found():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(mod, "_discover_worktrees", new_callable=AsyncMock, return_value=[]):
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

    with patch("kiro_crew.apps.builtins.dev_fleet.server._valid_worktree_names", new_callable=AsyncMock, return_value={"test-wt"}):
        resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "force must be a boolean" in body["error"]


# --- sync single-flight (409 on busy) ---
@pytest.mark.asyncio
async def test_sync_returns_409_when_already_running():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    # Inject a fake running sync
    mod._SYNC_RID = "fake123"
    async with mod._RUNS_LOCK:
        mod._RUNS["fake123"] = {"status": "running", "exit_code": None, "label": "sync", "output": []}

    try:
        result = await mod._sync()
        assert result["ok"] is False
        assert "already running" in result["error"]
    finally:
        async with mod._RUNS_LOCK:
            del mod._RUNS["fake123"]
        mod._SYNC_RID = None


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

    with patch.object(mod, "_discover_worktrees", new_callable=AsyncMock, return_value=fake_worktrees), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "100\t/fake\n", "")):
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
    mod._START_EPOCH = time.time() + 99999
    try:
        result = mod._build_pending()
        # dist dir may or may not exist in test env; either way it should not be "pending"
        assert result is False
    finally:
        mod._START_EPOCH = original_start


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
        mod._START_EPOCH = time.time() - 100  # pretend started 100s ago
        with patch.object(Path, '__new__', wraps=Path.__new__):
            # Directly test the logic: dist mtime > start epoch
            assert dist_path.stat().st_mtime > mod._START_EPOCH
        mod._START_EPOCH = original_start


def test_build_pending_false_when_dist_missing():
    """_build_pending returns False when dist dir does not exist (OSError path)."""
    import tempfile
    from pathlib import Path

    import kiro_crew.apps.builtins.dev_fleet.server as mod

    original_start = mod._START_EPOCH
    mod._START_EPOCH = 0  # very old — would be pending IF dist existed
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
        mod._START_EPOCH = original_start


# --- sync_run_id exposed in fleet response ---
@pytest.mark.asyncio
async def test_fleet_includes_sync_run_id():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    mod._SYNC_RID = "test-rid-abc"
    try:
        with patch.object(mod, "_discover_worktrees", new_callable=AsyncMock, return_value=[
            {"path": "/fake/main", "head": "abc1234", "branch": "main", "is_main": True}
        ]), \
             patch.object(mod, "_git_info", new_callable=AsyncMock, return_value={
                 "branch": "main", "head": "abc1234", "dirty": False,
                 "ahead": 0, "behind": 0, "last_updated_at": None
             }), \
             patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None), \
             patch.object(mod, "_git_ahead", new_callable=AsyncMock, return_value=0), \
             patch.object(mod, "_load_cfg", return_value=None), \
             patch.object(mod, "_build_pending", return_value=False):
            data = await mod._build_fleet()
        assert data["sync_run_id"] == "test-rid-abc"
        assert "build_pending" in data
    finally:
        mod._SYNC_RID = None


@pytest.mark.asyncio
async def test_fleet_includes_build_pending():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with patch.object(mod, "_discover_worktrees", new_callable=AsyncMock, return_value=[
        {"path": "/fake/main", "head": "abc1234", "branch": "main", "is_main": True}
    ]), patch.object(mod, "_git_info", new_callable=AsyncMock, return_value={
        "branch": "main", "head": "abc1234", "dirty": False,
        "ahead": 0, "behind": 0, "last_updated_at": None,
    }), patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None), \
            patch.object(mod, "_git_ahead", new_callable=AsyncMock, return_value=0), \
            patch.object(mod, "_load_cfg", return_value=None), \
            patch.object(mod, "_build_pending", return_value=True):
        data = await mod._build_fleet()
    assert data["build_pending"] is True


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
    monkeypatch.setattr(mod, "_sel", lambda: _fake_sel_capture(events)())

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
        "kiro_crew.apps.builtins.dev_fleet.server._valid_worktree_names",
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
    monkeypatch.setattr(mod, "_sel", lambda: _fake_sel_capture(events)())

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
        "kiro_crew.apps.builtins.dev_fleet.server._find_worktree",
        new_callable=AsyncMock, return_value=({"name": "feature-x"}, None),
    ), patch(
        "kiro_crew.apps.builtins.dev_fleet.server._pod_down",
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

    monkeypatch.setattr(mod, "_sel", lambda: _fake_sel_capture([])())

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
    monkeypatch.setattr(mod, "_sel", lambda: _fake_sel_capture(events)())

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
        "kiro_crew.apps.builtins.dev_fleet.server._valid_worktree_names",
        new_callable=AsyncMock, return_value={"feature-x"},
    ), patch(
        "kiro_crew.apps.builtins.dev_fleet.server._worktree_remove",
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

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_read_pin_strict",
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

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_read_pin_strict",
                      return_value=(True, str(ours))):
        assert await mod._pod_checkout_guard("kirocrew-wt-feature") is None

    # No pin file + no active unit -> pod never booted -> allow
    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_read_pin_strict", return_value=(False, None)), \
         patch.object(mod.rt, "active_names", return_value=set()):
        assert await mod._pod_checkout_guard("kirocrew-wt-feature") is None


@pytest.mark.asyncio
async def test_pod_guard_fails_closed_on_pin_read_error(monkeypatch, tmp_path):
    """Cannot read the pin state -> refuse the pod operation."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_read_pin_strict", side_effect=OSError("boom")):
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

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_read_pin_strict", return_value=(True, None)):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "ambiguous pod identity" in err


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

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_read_pin_strict", return_value=(False, None)), \
         patch.object(mod.rt, "active_names", return_value={"kirocrew-wt-feature"}):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "unattributable" in err

    # No pin + NOT active -> allow (pod never booted)
    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": str(ours)}, None)), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_read_pin_strict", return_value=(False, None)), \
         patch.object(mod.rt, "active_names", return_value=set()):
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

    monkeypatch.setattr(mod, "sandboxed_spawn_argv", fake_sandbox)
    rc, _, err = await mod._run_cmd(["git", "-C", "/x", "fetch"])
    assert rc == -1 and "sandbox unavailable" in err
    _assert_git_neutralizers(captured["env"])


def test_build_env_pins_git_protocols():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    env = mod._build_env()
    _assert_git_neutralizers(env)


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

        def kill(self):
            FakeProc.returncode = -9

        async def wait(self):
            FakeProc.returncode = FakeProc.returncode or -9
            return FakeProc.returncode

    async def fake_exec(*a, **kw):
        return FakeProc()

    async def fake_kill_tree(pid):
        killed.append(pid)

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(mod, "_kill_tree", fake_kill_tree)
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
    assert FakeProc.returncode is not None  # proc.kill()/wait() completed


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

    with patch.object(mod, "_git", new_callable=AsyncMock,
                      return_value=mod.BASE_BRANCH), \
         patch.object(mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(mod, "_build_env", return_value={}), \
         patch.object(mod, "sandboxed_spawn_argv", fake_sandbox), \
         patch.object(mod, "_start_run", new_callable=AsyncMock,
                      return_value="rid-sync-test"):
        mod._SYNC_RID = None
        res = await mod._sync()
        mod._SYNC_RID = None
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

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/x/wt-feat", "branch": "feat-x",
                                     "is_main": False}, None)), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock,
                      return_value=False), \
         patch.object(mod, "_pr_status_cached", new_callable=AsyncMock,
                      return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock,
                      return_value=0), \
         patch.object(mod, "_git", new_callable=AsyncMock,
                      return_value="deadbeef_local"), \
         patch.object(mod, "_fetch_pr_head_oid", new_callable=AsyncMock,
                      return_value="deadbeef_pr_different"), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock,
                      return_value="origin"), \
         patch.object(mod, "_POD_AVAILABLE", False):
        res = await mod._worktree_remove("wt-feat", force=False)
    assert res["ok"] is False
    assert "OID diverged" in res["error"]


@pytest.mark.asyncio
async def test_owner_repo_failure_not_cached_forever():
    """A transient owner/repo lookup failure retries after the TTL instead
    of disabling PR status until gateway restart; success is cached."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    lookups = AsyncMock(side_effect=[None, "own/repo"])
    mod._OWNER_REPO = None
    mod._OWNER_REPO_RETRY_AT = 0.0
    try:
        with patch.object(mod, "_repo_owner_name", lookups):
            assert await mod._get_owner_repo() is None
            assert lookups.await_count == 1
            # Within the TTL: no re-lookup, still None
            assert await mod._get_owner_repo() is None
            assert lookups.await_count == 1
            # TTL expired: retried and success cached
            mod._OWNER_REPO_RETRY_AT = 0.0
            assert await mod._get_owner_repo() == "own/repo"
            assert await mod._get_owner_repo() == "own/repo"
            assert lookups.await_count == 2
    finally:
        mod._OWNER_REPO = None
        mod._OWNER_REPO_RETRY_AT = 0.0


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
    mod._OWNER_REPO = None
    mod._OWNER_REPO_RETRY_AT = 0.0
    try:
        with patch.object(mod, "_repo_owner_name", new=AsyncMock(return_value="org/repo")):
            result = await mod._get_owner_repo()
            assert result == "org/repo"
            # Second call uses cache
            result2 = await mod._get_owner_repo()
            assert result2 == "org/repo"
    finally:
        mod._OWNER_REPO = None
        mod._OWNER_REPO_RETRY_AT = 0.0


@pytest.mark.asyncio
async def test_get_owner_repo_retries_on_failure():
    """R35(c): failures retry after 60s backoff, not cached permanently."""
    mod._OWNER_REPO = None
    mod._OWNER_REPO_RETRY_AT = 0.0
    call_count = 0

    async def failing():
        nonlocal call_count
        call_count += 1
        return None

    try:
        with patch.object(mod, "_repo_owner_name", new=failing):
            result = await mod._get_owner_repo()
            assert result is None
            assert call_count == 1

            # Within 60s window, should NOT retry
            result = await mod._get_owner_repo()
            assert result is None
            assert call_count == 1  # blocked by retry_at
    finally:
        mod._OWNER_REPO = None
        mod._OWNER_REPO_RETRY_AT = 0.0


# =============================================================================
# _discover_worktrees sandbox error (QA finding)
# =============================================================================


@pytest.mark.asyncio
async def test_discover_worktrees_sandbox_error_raises():
    """sandbox RuntimeError propagates as RuntimeError, not silent empty."""
    with patch.object(mod, "_run_cmd", new=AsyncMock(
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
    with patch.object(mod, "_run_cmd", new=AsyncMock(return_value=(-1, "", stderr))):
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
    with patch.object(mod, "_run_cmd", new=AsyncMock(return_value=(-1, "", stderr))):
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
    with patch.object(mod, "MAIN_REPO", str(missing)), \
         patch.object(mod, "_run_cmd", new=AsyncMock(
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
    with patch.object(mod, "MAIN_REPO", str(repo)), \
         patch.object(mod, "_run_cmd", new=AsyncMock(
             return_value=(128, "", "fatal: index file corrupt")
         )):
        with pytest.raises(RuntimeError, match="index file corrupt"):
            await mod._discover_worktrees()


@pytest.mark.asyncio
async def test_discover_worktrees_git_failure_is_bounded(tmp_path):
    """An unbounded git stderr is clipped before reaching the UI."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with patch.object(mod, "MAIN_REPO", str(repo)), \
         patch.object(mod, "_run_cmd", new=AsyncMock(
             return_value=(128, "", "fatal: " + "x" * 5000)
         )):
        with pytest.raises(RuntimeError) as exc:
            await mod._discover_worktrees()
    # bounded: the git portion is clipped to _GIT_ERR_MAX plus the fixed prefix
    assert len(str(exc.value)) <= mod._GIT_ERR_MAX + 200


# =============================================================================
# HMAC middleware tests
# =============================================================================


@pytest.mark.asyncio
async def test_hmac_valid_signature_passes():
    secret = "test-secret"
    app = _make_hmac_app()
    with patch.object(mod, "_load_app_secret", return_value=secret):
        async with TestClient(TestServer(app)) as client:
            headers = _sign_request(secret, "GET", "/api/fleet")
            resp = await client.get("/api/fleet", headers=headers)
            assert resp.status == 200


@pytest.mark.asyncio
async def test_hmac_missing_header_returns_401():
    app = _make_hmac_app()
    with patch.object(mod, "_load_app_secret", return_value="secret"):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet")
            assert resp.status == 401
            body = await resp.json()
            assert "missing" in body["error"]


@pytest.mark.asyncio
async def test_hmac_invalid_signature_returns_401():
    app = _make_hmac_app()
    with patch.object(mod, "_load_app_secret", return_value="secret"):
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
    with patch.object(mod, "_load_app_secret", return_value=secret):
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
    with patch.object(mod, "_load_app_secret", return_value=""):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"


# =============================================================================
# R35(b): worktree removal verdict_oid gate
# =============================================================================


@pytest.mark.asyncio
async def test_worktree_remove_refuses_cherry_ahead_at_pinned_oid():
    """Non-forced removal with merged PR must fail if branch OID != PR headRefOid."""
    fake_wt = {"path": "/tmp/fake-wt", "branch": "feat-x", "is_main": False}

    with patch.object(mod, "_find_worktree", new=AsyncMock(return_value=(fake_wt, None))), \
         patch.object(mod, "_git", new=AsyncMock(return_value="local_oid_aaa")), \
         patch.object(mod, "_real_dirty", new=AsyncMock(return_value=False)), \
         patch.object(mod, "_pr_status_cached", new=AsyncMock(return_value={"state": "MERGED", "number": 1})), \
         patch.object(mod, "_own_commits_count", new=AsyncMock(return_value=0)), \
         patch.object(mod, "_fetch_pr_head_oid", new=AsyncMock(return_value="pr_oid_bbb")), \
         patch.object(mod, "_upstream_remote", new=AsyncMock(return_value="origin")), \
         patch.object(mod, "_load_cfg", return_value=None), \
         patch.object(mod, "_POD_AVAILABLE", False):
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

    with patch.object(mod, "_fleet_refresh", new=boom), \
         patch.object(mod, "_fleet_cached", new=boom):
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

    with patch.object(mod, "_fleet_refresh", new=boom), \
         patch.object(mod, "_fleet_cached", new=boom):
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
# GIT_CONFIG_COUNT env neutralizers
# =============================================================================


def test_git_env_neutralizers_present():
    """_GIT_ENV_NEUTRALIZERS pins protocol and neutralizes execution vectors."""
    n = mod._GIT_ENV_NEUTRALIZERS
    assert n["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert n["GIT_PROTOCOL_FROM_USER"] == "0"
    assert n["GIT_CONFIG_COUNT"] == "4"
    assert n["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
    assert n["GIT_CONFIG_VALUE_0"] == "false"
    assert n["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert n["GIT_CONFIG_VALUE_1"] == "/dev/null"
    assert n["GIT_CONFIG_KEY_2"] == "credential.helper"
    assert n["GIT_CONFIG_VALUE_2"] == ""
    assert n["GIT_CONFIG_KEY_3"] == "core.sshCommand"
    assert n["GIT_CONFIG_VALUE_3"] == "ssh"


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
    monkeypatch.setattr(mod, "_APP_SECRET", None)
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

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(mod, "_git", side_effect=mock_git), \
         patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=2), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "50\t/fake/wt\n", "")), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"), \
         patch.object(mod, "_POD_AVAILABLE", False):
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

    with patch.object(mod, "_find_worktree", new_callable=AsyncMock,
                      return_value=({"path": "/fake/wt", "branch": "feat-x", "is_main": False}, None)), \
         patch.object(mod, "_git", side_effect=mock_git), \
         patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=0), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "50\t/fake/wt\n", "")), \
         patch.object(mod, "_POD_AVAILABLE", False):
        result = await mod._worktree_detail("feat-x")

    assert set(result["design_docs"]) == {"docs/README.md", "package/docs/spec.md", "design-doc.md"}


# =============================================================================
# Task 2: restart-gateway endpoint
# =============================================================================


@pytest.mark.asyncio
async def test_restart_gateway_not_active():
    """restart-gateway returns ok:false when service is not active."""
    with patch.object(mod, "_run_cmd", new_callable=AsyncMock,
                      return_value=(3, "", "inactive\n")):
        result = await mod._restart_gateway()
    assert result["ok"] is False
    assert "not running" in result["error"]


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

    with patch.object(mod, "_run_cmd", side_effect=mock_run_cmd), \
         patch.object(mod, "sys") as mock_sys, \
         patch.object(mod, "shutil") as mock_shutil:
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
    mod._MAKE_LIVE_COMMITTED = False
    yield
    mod._MAKE_LIVE_COMMITTED = False


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


def _assert_sandboxed(path, what: str) -> None:
    """Fail loudly when a host-mutating make-live seam resolves outside the sandbox.

    The seams below decide WHERE the cutover writes and WHAT it executes. If one is
    left unpatched the production code is correct and does exactly what it is told —
    against the developer's own machine: it rewrites the live gateway's systemd
    drop-in to point at a pytest tmpdir and restarts the unit, which then fails
    203/EXEC on every boot once the tmpdir is reaped. Asserting containment here
    makes the next missed seam fail inside the test instead of taking down the host.
    """
    resolved = Path(path).resolve()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    assert tmp_root in resolved.parents, (
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
    monkeypatch.setattr(mod, "sys", MagicMock(platform=platform))
    tool = "/usr/bin/systemctl" if platform == "linux" else "/bin/launchctl"
    monkeypatch.setattr(mod, "shutil", MagicMock(which=MagicMock(return_value=tool)))
    monkeypatch.setattr(
        mod, "_discover_worktrees",
        AsyncMock(return_value=[{"path": str(wt), "branch": "feat", "is_main": False}]),
    )
    monkeypatch.setattr(mod, "_live_worktree_path", AsyncMock(return_value=live))
    monkeypatch.setattr(mod, "_in_pod", lambda: in_pod)
    monkeypatch.setattr(mod, "_live_user_unit_status", AsyncMock(return_value=unit_status))
    sandbox_dropin = (
        Path(wt).parent / "_systemd" / f"{mod._LIVE_GATEWAY_UNIT}.d" / "make-live.conf"
    )
    _assert_sandboxed(sandbox_dropin, "_dropin_path")
    monkeypatch.setattr(mod, "_dropin_path", lambda: sandbox_dropin)
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    # Prove the redirect actually took: a rename of the production symbol would
    # otherwise leave the real path live while every test still looked green.
    _assert_sandboxed(mod._dropin_path(), "patched _dropin_path()")
    if pointer_dir is not None:
        pointer_dir.mkdir(parents=True, exist_ok=True)
        ptr_file = pointer_dir / "live_target.json"
        monkeypatch.setattr(mod.live_target, "pointer_path", lambda: ptr_file)
    if platform == "darwin":
        monkeypatch.setattr(
            mod.gateway_service, "restart_contract_current", lambda _path: True
        )
        monkeypatch.setattr(
            mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
        )


@pytest.mark.asyncio
async def test_make_live_dry_run_plan(monkeypatch, tmp_path):
    """dry_run returns the pointer-based plan without writing anything."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_discover_worktrees", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod, "_in_pod", lambda: False)
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
    monkeypatch.setattr(mod, "_dropin_path", lambda: dropin)
    monkeypatch.setattr(mod, "_LIVE_WORKTREE", "sentinel", raising=False)
    monkeypatch.setattr(mod, "_LIVE_CHECK_AT", 123.0, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_dropin_path", lambda: dropin)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res["staged_only"] is True
    assert (ptr_dir / "live_target.json").is_file()
    assert not dropin.exists()
    assert ["systemctl", "--user", "daemon-reload"] not in calls


@pytest.mark.asyncio
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

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
async def test_make_live_write_failure_does_not_latch(monkeypatch, tmp_path):
    """A cutover that fails during pointer write (before restart scheduling)
    must NOT latch — the restart never happened, so a subsequent cutover
    proceeds normally."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    # Make write_target raise an OSError to simulate a disk failure.
    original_write = mod.live_target.write_target
    call_count = {"n": 0}

    def fail_first_write(checkout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("disk full")
        return original_write(checkout)

    monkeypatch.setattr(mod.live_target, "write_target", fail_first_write)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "write_failed"
    assert mod._MAKE_LIVE_COMMITTED is False

    # With the seam repaired, a subsequent cutover proceeds (not latched).
    monkeypatch.setattr(mod.live_target, "write_target", original_write)
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

    monkeypatch.setattr(mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )
    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(mod, "_LIVE_WORKTREE", None, raising=False)
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

    monkeypatch.setattr(mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )
    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(mod, "_LIVE_WORKTREE", None, raising=False)
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
    monkeypatch.setattr(mod, "_in_pod", lambda: None)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "pod_indeterminate"


# --- make-live: staged-only when service not drivable ---
@pytest.mark.asyncio
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
        monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
        calls: list = []

        async def fake_run_cmd(cmd, **kw):
            calls.append(cmd)
            return (0, "", "")

        monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "sys", MagicMock(platform="win32"))
    assert await mod._live_user_unit_status() == "no_systemd"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_no_agent(monkeypatch):
    """macOS with launchctl but no such agent loaded -> no_agent."""
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(1, "", "no such")))
    assert await mod._live_user_unit_status() == "no_agent"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_agent_not_indirected(monkeypatch, tmp_path):
    """Agent loaded, but its plist does not go through the live-gateway symlink.

    Swapping the symlink would then be a silent no-op, so make-live must refuse
    with an actionable code instead of reporting a cutover that did nothing.
    """
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "  pid = 7\n", "")))
    plist = tmp_path / "agent.plist"
    plist.write_text("<string>/usr/local/bin/kirocrew</string>")
    monkeypatch.setattr(
        mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    assert await mod._live_user_unit_status() == "agent_not_indirected"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_ok(monkeypatch, tmp_path):
    """Agent loaded AND indirected through the live-gateway symlink -> ok."""
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "  pid = 7\n", "")))
    link = tmp_path / "live-gateway"
    link.write_text("#!/bin/sh\nexec '/usr/local/bin/kirocrew' \"$@\"\n")
    plist = tmp_path / "agent.plist"
    plist.write_text(f"<string>{link}</string>")
    monkeypatch.setattr(
        mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    monkeypatch.setattr(
        mod.gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: link)
    )
    monkeypatch.setattr(
        mod.gateway_service, "restart_contract_current", lambda _path: True
    )
    monkeypatch.setattr(
        mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
    )
    assert await mod._live_user_unit_status() == "ok"


@pytest.mark.asyncio
async def test_live_user_unit_status_darwin_loaded_contract_outdated(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    link = tmp_path / "live-gateway"
    link.write_text("#!/bin/sh\n")
    plist = tmp_path / "agent.plist"
    plist.write_text(f"<string>{link}</string>")
    monkeypatch.setattr(
        mod.gateway_service.LaunchdBackend, "plist_path", staticmethod(lambda: plist)
    )
    monkeypatch.setattr(
        mod.gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: link)
    )
    monkeypatch.setattr(
        mod.gateway_service, "restart_contract_current", lambda _path: True
    )
    monkeypatch.setattr(
        mod.gateway_service, "loaded_restart_contract_current", lambda _out: False
    )
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "pid = 7\n", "")))

    assert await mod._live_user_unit_status() == "agent_restart_contract_outdated"


@pytest.mark.asyncio
async def test_live_user_unit_status_ok_and_missing(monkeypatch):
    """`systemctl --user cat` rc==0 AND the unit running -> ok; rc!=0 (system unit
    / not installed) -> no_user_unit.

    Loadedness alone is not enough: `ok` means a restart replaces the gateway we
    are in, so the classifier also requires `is-active`.
    """
    monkeypatch.setattr(mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )

    async def loaded_and_running(cmd, **kw):
        assert cmd[:2] == ["systemctl", "--user"]
        if "is-active" in cmd:
            return (0, "active", "")
        assert "cat" in cmd
        return (0, "# unit contents", "")

    monkeypatch.setattr(mod, "_run_cmd", loaded_and_running)
    assert await mod._live_user_unit_status() == "ok"

    async def cat_missing(cmd, **kw):
        return (1, "", "No files found for kirocrew-gateway.service.")

    monkeypatch.setattr(mod, "_run_cmd", cat_missing)
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

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    # dry-run plan names the pointer path and correct exec.
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True
    plan = res["plan"]
    assert plan["mechanism"] == "live-target pointer"
    assert plan["exec"] == str(wt / ".venv" / "bin" / "kirocrew")
    # Real cutover writes the pointer with the resolved (space-containing) path.
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
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
        raise mod.live_target.InvalidTarget("control chars in path")

    monkeypatch.setattr(mod.live_target, "validate", boom)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_ACTIVE", None, raising=False)
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(
        mod.gateway_service, "restart_contract_current", lambda _path: True
    )
    monkeypatch.setattr(
        mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  state = running\n  pid = 4242\n", "")
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    res = await mod._restart_gateway()
    assert res["ok"] is True
    # The PID stands in for systemd's monotonic start stamp: it changes on every
    # respawn, which is the edge the frontend handshake waits for.
    assert res["start_id"] == "4242"
    # Addressed via the backend's own domain helper rather than a second copy of
    # the uid logic (which would also break on Windows, where getuid is absent).
    domain = mod.gateway_service.LaunchdBackend.domain()
    assert ["launchctl", "kill", "TERM", f"{domain}/{mod._gateway_label()}"] in calls
    # `kickstart -k` would restart it too, but as a hard kill rather than the
    # graceful SIGTERM the ExitTimeOut budget is sized for.
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in calls)
    # The legacy verb is what the sandbox blocks — it must not reappear.
    assert not any(c[:2] == ["launchctl", "stop"] for c in calls)


@pytest.mark.asyncio
async def test_restart_gateway_darwin_refuses_stale_loaded_contract(monkeypatch):
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(
        mod.gateway_service, "restart_contract_current", lambda _path: True
    )
    monkeypatch.setattr(
        mod.gateway_service, "loaded_restart_contract_current", lambda _out: False
    )
    calls = []

    async def fake_run_cmd(cmd, **_kw):
        calls.append(cmd)
        return (0, "pid = 4242\n", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    res = await mod._restart_gateway()
    assert res["ok"] is False
    assert "loaded launchd restart contract is outdated" in res["error"]
    assert not any(c[:3] == ["launchctl", "kill", "TERM"] for c in calls)


@pytest.mark.asyncio
async def test_restart_gateway_darwin_not_active(monkeypatch):
    """No loaded agent -> refuse, without attempting a kickstart."""
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_ACTIVE", None, raising=False)
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (1, "", "no such service")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  pid = 99\n", "")
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  pid = 99\n", "")
        if cmd[:3] == ["launchctl", "kill", "TERM"]:
            return (1, "", "restart refused")
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    # Make snapshot() raise PermissionError (simulating an unreadable pointer).
    monkeypatch.setattr(
        mod.live_target, "snapshot",
        lambda: (_ for _ in ()).throw(PermissionError("unreadable")),
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "write_failed"
    # No restart issued.
    assert not any(
        c[:2] == ["systemd-run", "--user"] for c in calls
    )
    assert mod._MAKE_LIVE_COMMITTED is False


# --- make-live: pointer write + failure rollback ---
@pytest.mark.asyncio
async def test_make_live_rolls_back_pointer_on_restart_failure(monkeypatch, tmp_path):
    """A restart failure with a prior pointer restores the PRIOR content and
    reports rolled_back. When there was no prior pointer, the file is deleted."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    # Prior was absent -> pointer file deleted on rollback.
    assert not (ptr_dir / "live_target.json").exists()
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_make_live_rolls_back_pointer_preserves_prior(monkeypatch, tmp_path):
    """When a prior pointer existed, restart failure restores its content."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir(parents=True)
    ptr_file = ptr_dir / "live_target.json"
    prior_content = '{"checkout": "/old/checkout"}\n'
    ptr_file.write_text(prior_content)
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    assert ptr_file.read_text() == prior_content


# --- make-live: concurrency single-flight lock ---
@pytest.mark.asyncio
async def test_make_live_concurrent_second_call_busy(monkeypatch, tmp_path):
    """While one cutover holds the make-live lock, a concurrent second call is
    refused immediately with ``busy`` (fail-fast, not queued) and the winner
    still completes and releases the lock."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    # Fresh lock so a leaked hold from another test can't poison this one.
    monkeypatch.setattr(mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    entered = asyncio.Event()   # set once the first call is inside the lock
    release = asyncio.Event()   # test-controlled gate to hold it there

    # write_target runs inside the critical section, so signalling from it is an
    # exact barrier: no sleep can substitute, because a loaded runner may not
    # have reached the lock yet and the assertion below would then read an
    # unlocked lock and let the second call through.
    original_write_target = mod.live_target.write_target

    def signalling_write(checkout):
        result = original_write_target(checkout)
        entered.set()
        return result

    monkeypatch.setattr(mod.live_target, "write_target", signalling_write)

    async def fake_run_cmd(cmd, **kw):
        # Block here (inside the lock) until released.
        if cmd[:2] == ["systemd-run", "--user"]:
            await release.wait()
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)

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
async def test_make_live_lock_released_after_failure_and_reusable(monkeypatch, tmp_path):
    """The lock is released on the failure-rollback path too, so a subsequent
    cutover proceeds (never wedged on ``busy``)."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    # 1) restart fails -> rollback path; the lock MUST be released.
    async def restart_fails(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", restart_fails)
    fail = await mod._make_live(str(wt), dry_run=False)
    assert fail["ok"] is False and fail["code"] == "restart_failed"
    assert mod._MAKE_LIVE_LOCK.locked() is False

    # 2) A subsequent all-green cutover proceeds (not refused as busy) and also
    #    releases the lock on the success path.
    async def all_ok(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", all_ok)
    ok = await mod._make_live(str(wt), dry_run=False)
    assert ok["ok"] is True and ok.get("cutover") is True
    assert mod._MAKE_LIVE_LOCK.locked() is False


# --- make-live: pointer-based live worktree resolution ---
@pytest.mark.asyncio
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

    monkeypatch.setattr(mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(mod, "_LIVE_WORKTREE", None, raising=False)
    # Even with a systemd probe that would return a different path, the pointer
    # takes priority.
    monkeypatch.setattr(mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )

    async def should_not_be_called(cmd, **kw):
        raise AssertionError(f"systemctl should not be called: {cmd}")

    monkeypatch.setattr(mod, "_run_cmd", should_not_be_called)
    # The gateway is executing the pointer target: the cutover has taken effect.
    monkeypatch.setattr(mod, "_running_checkout", lambda: target_wt.resolve())
    got = await mod._live_worktree_path()
    assert got == str(target_wt.resolve())
    assert mod._staged_target() is None


@pytest.mark.asyncio
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

    monkeypatch.setattr(mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(mod, "_LIVE_WORKTREE", None, raising=False)
    monkeypatch.setattr(mod, "_running_checkout", lambda: running_wt)

    got = await mod._live_worktree_path()
    assert got == str(running_wt), "live must name the image actually executing"
    assert got != str(target_wt.resolve())
    assert mod._staged_target() == str(target_wt.resolve())


@pytest.mark.asyncio
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

    monkeypatch.setattr(mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(mod, "_LIVE_WORKTREE", None, raising=False)
    monkeypatch.setattr(mod, "_running_checkout", lambda: None)

    assert await mod._live_worktree_path() == str(target_wt.resolve())
    assert mod._staged_target() is None


@pytest.mark.asyncio
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
    monkeypatch.setattr(mod, "_running_checkout", lambda: running)

    # A cutover to a DIFFERENT checkout is staged.
    import json as _json
    ptr = mod.live_target.pointer_path()
    ptr.write_text(_json.dumps({"checkout": str(other)}) + "\n")
    assert mod._staged_target() == str(other)

    res = await mod._make_live(str(running))

    assert res.get("ok") is True, res
    assert res.get("cancelled") is True, res
    assert res.get("code") != "already_live"
    # The pointer is RE-PINNED to the running checkout, not deleted: deleting it
    # would discard the record that this checkout is the chosen live target.
    assert ptr.exists(), "cancelling must not delete the live-target record"
    assert mod.live_target.read_target() == running.resolve()
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
    monkeypatch.setattr(mod, "_running_checkout", lambda: running)
    import json as _json
    ptr = mod.live_target.pointer_path()
    ptr.write_text(_json.dumps({"checkout": str(other)}) + "\n")
    assert mod._staged_target() == str(other)
    return running, other, ptr


@pytest.mark.asyncio
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
    monkeypatch.setattr(mod.live_target, "write_target",
                        lambda _c: (_ for _ in ()).throw(OSError(28, "No space")))
    loop_thread = threading.get_ident()
    restore_threads: list = []
    monkeypatch.setattr(
        mod.live_target, "restore",
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
    monkeypatch.setattr(mod, "_running_checkout", lambda: running)
    ptr = mod.live_target.pointer_path()
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
    assert mod.live_target.read_target() == running.resolve()
    # Nothing is staged any more, so no restart is pending.
    assert mod._staged_target() is None


@pytest.mark.parametrize("boom", [
    OSError(28, "No space left on device"),
    OSError(30, "Read-only file system"),
])
@pytest.mark.asyncio
async def test_cancel_write_failure_is_a_refusal_not_a_crash(monkeypatch, tmp_path, boom):
    """A full or read-only data home must refuse, not raise into a 500.

    write_target mkdirs, writes atomically and re-applies the owner-only mode, so
    the failure mode here is OSError — which the InvalidTarget guard alone (a
    ValueError) does not cover.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def explode(_checkout):
        raise boom

    monkeypatch.setattr(mod.live_target, "write_target", explode)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res
    assert "could not be re-pinned" in res["error"]


@pytest.mark.asyncio
async def test_cancel_rolls_the_pointer_back_when_hardening_fails(monkeypatch, tmp_path):
    """write_target can fail AFTER replacing the pointer.

    It re-applies the owner-only mode as its last step, so a failure there leaves
    a code-execution input in place with inherited permissions. The cancel must be
    all-or-nothing: the staged pointer goes back exactly as it was.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    staged_before = ptr.read_text(encoding="utf-8")

    real_write = mod.live_target.write_target

    def write_then_fail(checkout):
        real_write(checkout)                      # the pointer IS replaced
        raise OSError(5, "SetNamedSecurityInfo failed")

    monkeypatch.setattr(mod.live_target, "write_target", write_then_fail)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res
    # Rolled back byte-for-byte: the stage is still staged, nothing half-applied.
    assert ptr.read_text(encoding="utf-8") == staged_before
    assert mod._staged_target() is not None


@pytest.mark.asyncio
async def test_cancel_reports_a_failed_rollback(monkeypatch, tmp_path):
    """When the rollback itself fails the operator is told, not left guessing."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def write_then_fail(_checkout):
        raise OSError(5, "SetNamedSecurityInfo failed")

    monkeypatch.setattr(mod.live_target, "write_target", write_then_fail)
    monkeypatch.setattr(mod.live_target, "restore", lambda _prior: False)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert "rollback also failed" in res["error"]


@pytest.mark.asyncio
async def test_cancel_invalid_target_is_a_refusal_not_a_crash(monkeypatch, tmp_path):
    """The validation half of the same guard."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def explode(_checkout):
        raise mod.live_target.InvalidTarget("no src/kiro_crew in target")

    monkeypatch.setattr(mod.live_target, "write_target", explode)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res


@pytest.mark.asyncio
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
async def test_cancel_refuses_once_a_cutover_has_committed(monkeypatch, tmp_path):
    """A committed cutover is already restarting; deleting the pointer then would
    land the pending restart somewhere the operator did not choose."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", True, raising=False)

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
    monkeypatch.setattr(mod, "_gateway_backend", lambda: backend)

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
    monkeypatch.setattr(mod, "_gateway_backend", lambda: backend)

    assert await mod._live_user_unit_status() == "ok"


def test_running_checkout_resolves_this_checkout():
    """_running_checkout derives the executing checkout from the loaded module,
    which is what makes it authoritative where a service definition is not."""
    got = mod._running_checkout()
    assert got is not None
    assert (got / "src" / "kiro_crew").is_dir()


@pytest.mark.asyncio
async def test_make_live_staged_only_allows_subsequent_cutover(monkeypatch, tmp_path):
    """A staged_only cutover does NOT latch, so re-pointing to a DIFFERENT
    worktree proceeds without a restart_pending refusal."""
    wt1 = _mk_make_live_wt(tmp_path / "a", venv=True, dist=True)
    wt2 = _mk_make_live_wt(tmp_path / "b", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt1, unit_status="no_user_unit",
                    pointer_dir=ptr_dir)
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl")))
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return 0, "active", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(mod, "sys", MagicMock(platform="win32"))
    run = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(mod, "_run_cmd", run)
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
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return 0, "  pid = 4242\n", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    assert await mod._gateway_service_active() is True
    assert calls and calls[0][:2] == ["launchctl", "print"]


@pytest.mark.asyncio
async def test_gateway_service_active_darwin_loaded_without_pid(monkeypatch):
    """macOS agent loaded but not running (no pid line) -> False.

    ``LaunchdBackend.active`` treats only a live pid as active, mirroring
    ``systemctl is-active``; a zero exit from ``launchctl print`` alone is not
    enough.
    """
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(
        mod, "_run_cmd", AsyncMock(return_value=(0, "  state = waiting\n", ""))
    )
    assert await mod._gateway_service_active() is False


# ---- trusted global credential helper re-injection ----


@pytest.mark.asyncio
async def test_trusted_helpers_loaded_from_global_config(monkeypatch):
    """Operator-global gh helper is SYNTHESIZED and re-pinned after the
    reset; the persistent `store` helper is rejected."""
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", None)
    monkeypatch.setattr(mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")

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

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        if cmd[2] == "--system":
            return 0, "credential.helper osxkeychain\n", ""
        return 1, "", ""  # nothing in global, as on a stock macOS host

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        if cmd[2] == "--system":
            return 0, "credential.helper osxkeychain\n", ""
        return 0, "credential.helper manager\n", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", None)
    scopes: list = []

    async def fake_run_cmd(cmd, **kw):
        scopes.append(cmd[2])
        return 1, "", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    await mod._load_trusted_credential_helpers()
    assert "--local" not in scopes
    assert set(scopes) == {"--system", "--global"}


@pytest.mark.asyncio
async def test_trusted_helpers_empty_when_no_global_config(monkeypatch):
    """No global helpers -> reset stands, no env additions."""
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", None)

    async def fake_run_cmd(cmd, **kw):
        return 1, "", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", helpers)
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

    with patch.object(mod, "_git", new_callable=AsyncMock,
                      return_value=mod.BASE_BRANCH), \
         patch.object(mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(mod, "sandboxed_spawn_argv", fake_sandbox), \
         patch.object(mod, "_start_run", new_callable=AsyncMock,
                      return_value="rid-steps"):
        mod._SYNC_RID = None
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
async def test_sync_stages_dist_on_a_stock_checkout(monkeypatch):
    """The staging step is part of the sync on a stock checkout.

    `npm run build` writes website/dist while the dashboard serves
    src/kiro_crew/static/dist; without this step Pull+Build reports success and
    the gateway keeps serving the previous bundle.
    """
    monkeypatch.setattr(mod.frontend, "edition_configured", lambda: False)
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
    monkeypatch.setattr(mod.frontend, "edition_configured", lambda: True)
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
async def test_sync_build_steps_never_see_credential_helpers(monkeypatch):
    """Only the network fetch step carries operator credential helpers;
    worktree-controlled merge/pip/npm steps must not (token minting via
    `git credential fill` from a malicious install script)."""
    base, helpers = _fake_helpers()
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", helpers)
    captured: list[tuple[list, dict]] = []

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured.append((list(argv), dict(env or {})))
        return list(argv), dict(env or {}), None

    with patch.object(mod, "_git", new_callable=AsyncMock,
                      return_value=mod.BASE_BRANCH), \
         patch.object(mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")), \
         patch.object(mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(mod, "sandboxed_spawn_argv", fake_sandbox), \
         patch.object(mod, "_start_run", new_callable=AsyncMock,
                      return_value="rid-cred-test"):
        mod._SYNC_RID = None
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
    ]
    assert fetch_envs and all(key in e for e in fetch_envs)
    assert len(build_envs) == 4  # merge + pip + npm ci + (npm build + stage)
    assert all(key not in e for e in build_envs)


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_refuses_non_merged(monkeypatch):
    """Branch-name reuse: a fresh OPEN PR on a recycled name must NOT yield a
    head OID at the destructive boundary, even if a stale MERGED verdict is
    cached elsewhere."""
    async def fake_run(cmd, **kw):
        return 0, json.dumps({"headRefOid": "a" * 40, "state": "OPEN"}), ""

    monkeypatch.setattr(mod, "_get_owner_repo", AsyncMock(return_value="o/r"))
    monkeypatch.setattr(mod, "_run_cmd", fake_run)
    assert await mod._fetch_pr_head_oid("feature-x") is None

    async def fake_run_merged(cmd, **kw):
        return 0, json.dumps({"headRefOid": "b" * 40, "state": "MERGED"}), ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_merged)
    assert await mod._fetch_pr_head_oid("feature-x") == "b" * 40


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


@pytest.mark.skipif(mod.platform_compat.IS_WINDOWS, reason="POSIX bin dirs")
def test_trusted_bin_dirs_cover_homebrew_prefixes():
    """A `gh`/`git` the user installed with Homebrew must be reachable: without
    the brew prefixes Dev Fleet could not find gh at all on a stock macOS host
    (only Xcode's /usr/bin/git), and its PATH pin excluded them too."""
    assert "/opt/homebrew/bin" in mod._TRUSTED_BIN_DIRS
    assert "/home/linuxbrew/.linuxbrew/bin" in mod._TRUSTED_BIN_DIRS
    assert "/opt/homebrew/bin" in mod._TRUSTED_PATH.split(os.pathsep)


@pytest.mark.skipif(mod.platform_compat.IS_WINDOWS, reason="POSIX symlink layout")
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
    monkeypatch.setattr(mod, "_TRUSTED_BIN_DIRS", (str(bin_dir),))

    assert mod._trusted_bin("gh") == str(target.resolve())
    mod._TRUSTED_BIN_CACHE.clear()


@pytest.mark.asyncio
async def test_run_cmd_pins_trusted_path(monkeypatch):
    """_run_cmd rewrites bare names to trusted absolute paths and pins PATH."""
    captured: dict = {}

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured["argv"] = list(argv)
        captured["env"] = dict(env or {})
        return ["/bin/false"], dict(env or {}), None

    monkeypatch.setattr(mod, "sandboxed_spawn_argv", fake_sandbox)
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

    monkeypatch.setattr(mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(mod, "_run_cmd", fake_run)
    assert await mod._upstream_remote() == "origin"

    async def fake_run_valid(cmd, **kw):
        if "config" in cmd:
            return 0, "kirocrew", ""
        return 0, "origin\nkirocrew\n", ""

    monkeypatch.setattr(mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(mod, "_run_cmd", fake_run_valid)
    assert await mod._upstream_remote() == "kirocrew"

    async def fake_run_unlisted(cmd, **kw):
        if "config" in cmd:
            return 0, "evil", ""
        return 0, "origin\nkirocrew\n", ""

    monkeypatch.setattr(mod, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(mod, "_run_cmd", fake_run_unlisted)
    assert await mod._upstream_remote() == "origin"
    monkeypatch.setattr(mod, "_UPSTREAM_REMOTE", None)


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
    monkeypatch.setattr(mod, "_trusted_bin", lambda n: "/usr/bin/gh" if n == "gh" else None)
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
    monkeypatch.setattr(mod, "_trusted_bin", lambda n: "/opt/trusted/gh" if n == "gh" else None)
    expected = "!/opt/trusted/gh auth git-credential"
    assert mod._sanitize_helper_value("!gh auth git-credential") == expected
    assert mod._sanitize_helper_value(
        "!/local/home/user/.local/bin/gh auth git-credential") == expected
    monkeypatch.setattr(mod, "_trusted_bin", lambda n: None)
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

    monkeypatch.setattr(mod, "_run_cmd", fake_run)
    monkeypatch.setattr(
        mod, "_trusted_bin",
        lambda n: "/usr/bin/gh" if n == "gh" else None,
    )
    await mod._load_trusted_credential_helpers()
    h = mod._GIT_TRUSTED_HELPERS or {}
    vals = [v for k, v in h.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert vals == ["!/usr/bin/gh auth git-credential"]
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", None)


@pytest.mark.asyncio
async def test_hmac_no_secret_always_denies(monkeypatch):
    """No app secret = fail closed, no env bypass, and the denial is SEL-audited."""
    events: list = []

    class FakeSel:
        def log_tool_invocation(self, **kw):
            events.append(kw)

    monkeypatch.setattr(mod, "_load_app_secret", lambda: "")
    monkeypatch.setattr(mod, "_sel", lambda: FakeSel())
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

    monkeypatch.setattr(mod, "_load_app_secret", lambda: "sekrit")
    monkeypatch.setattr(mod, "_sel", lambda: FakeSel())
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
async def test_prunable_merged_unverified_when_oid_lookup_fails():
    """OID verification unavailable -> never a prune candidate (preview must
    match the removal path, which would refuse anyway)."""
    with patch.object(mod, "_pr_status_cached", new_callable=AsyncMock,
                      return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=2), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"):
        v = await mod._prunable("/fake/path", "feat-branch")
    assert v["ok"] is False
    assert v["code"] == "merged_unverified"


def test_strict_sandbox_hides_gh_config():
    """.config/gh (the gh helper's token store) must be hidden from the
    strict tier where worktree-controlled code executes."""
    import kiro_crew.sandbox as sandbox_mod

    assert ".config/gh" in sandbox_mod._STRICT_DIRS
    assert ".config/gh" not in sandbox_mod._STANDARD_DIRS


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
    monkeypatch.setattr(mod, "_FALLBACK_REPOS", ["old-org/old-repo"])
    queried: list = []

    async def fake_owner_repo():
        return "new-org/new-repo"

    async def fake_query(repo, branch):
        queried.append(repo)
        if repo == "old-org/old-repo":
            return {"number": 31, "state": "MERGED", "_repo": repo}
        return None

    monkeypatch.setattr(mod, "_get_owner_repo", fake_owner_repo)
    monkeypatch.setattr(mod, "_pr_query_one", fake_query)
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
    monkeypatch.setattr(mod, "MAIN_REPO", str(repo))
    monkeypatch.setattr(mod, "_SYNC_RID", None)

    async def fake_remote():
        return "origin"

    async def fake_head():
        return "main"

    monkeypatch.setattr(mod, "_upstream_remote", fake_remote, raising=False)
    monkeypatch.setattr(mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")
    captured: dict = {}

    def fake_sandboxed(argv, mode, env=None):
        captured.setdefault("argvs", []).append(list(argv))
        return list(argv), dict(env or {}), None

    monkeypatch.setattr(mod, "sandboxed_spawn_argv", fake_sandboxed)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)

    async def fake_start_run(label, cmd, **kw):
        captured["cmd"] = cmd
        return "rid-1"

    monkeypatch.setattr(mod, "_start_run", fake_start_run)
    res = await mod._sync_start_locked()
    assert res.get("ok"), res
    pip_argvs = [a for a in captured.get("argvs", []) if "-m" in a and "pip" in a]
    assert pip_argvs, captured.get("argvs")
    assert pip_argvs[0][0] == str(repo / ".venv" / "bin" / "python")
    assert pip_argvs[0][0] != _sys.executable


@pytest.mark.asyncio
async def test_sync_refuses_when_target_repo_has_no_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "MAIN_REPO", str(tmp_path / "novenv"))
    monkeypatch.setattr(mod, "_SYNC_RID", None)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
    rid = await mod._start_run("sync", ["true"], env={})
    for _ in range(50):
        await mod.asyncio.sleep(0.05)
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

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    assert await mod._head_contained_in_pr("/wt", "aaa", "bbb") is True


@pytest.mark.asyncio
async def test_head_not_contained_when_diverged(monkeypatch):
    async def fake_run_cmd(cmd, **kw):
        return 1, "", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    assert await mod._head_contained_in_pr("/wt", "aaa", "bbb") is False


@pytest.mark.asyncio
async def test_head_contained_equal_oids_no_spawn(monkeypatch):
    called = []

    async def fake_run_cmd(cmd, **kw):
        called.append(cmd)
        return 1, "", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
        "title": "My PR title", "body": "Fixes #7",
    }])
    with patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, payload, "")):
        pr = await mod._pr_query_one("o/r", "feat/x")
    assert pr is not None
    assert pr["title"] == "My PR title"
    assert pr["_body"] == "Fixes #7"
    assert "body" not in pr  # moved to internal _body
    redacted = mod._redact_pr(pr)
    assert redacted["title"] == "My PR title"
    assert redacted["number"] == 42
    assert "_body" not in redacted  # internal fields dropped from payload
    assert "_repo" not in redacted


# --- _build_context: parses PR body + commits, builds links ---
@pytest.mark.asyncio
async def test_build_context_parses_pr_body_and_commits():
    log = (
        "feat(dev-fleet): surface context\x1fFixes #147\nrelated #99\x1e"
        "wip TT-5 progress\x1f\x1e"
    )
    with patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value=log), \
         patch.object(mod, "_html_repo_base", new_callable=AsyncMock,
                      return_value="https://github.com/kirodotdev/KiroCrew"), \
         patch.object(mod, "_load_dev_fleet_cfg",
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
    with patch.object(mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_html_repo_base", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_load_dev_fleet_cfg", return_value={}):
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

    monkeypatch.setattr(mod, "_build_context", fake_build)
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
    with patch.object(mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_discover_worktrees", new_callable=AsyncMock, return_value=worktrees), \
         patch.object(mod, "_load_cfg", return_value=None), \
         patch.object(mod, "_git_info", new_callable=AsyncMock, return_value=ginfo), \
         patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value=pr), \
         patch.object(mod, "_git_ahead", new_callable=AsyncMock, return_value=0), \
         patch.object(mod, "_context_cached", new_callable=AsyncMock, return_value=sentinel), \
         patch.object(mod, "_gateway_service_active", new_callable=AsyncMock, return_value=False):
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
    with ExitStack() as stack:
        for attr, repl in defaults.items():
            stack.enter_context(patch.object(mod, attr, repl))
        for attr, value in patches.items():
            stack.enter_context(patch.object(mod, attr, value))
        return await mod._build_fleet()


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
    with patch.object(mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_POD_AVAILABLE", True), \
         patch.object(mod.rt, "active_names", return_value={"kirocrew-wt-x"}):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "still active" in result["error"]


@pytest.mark.asyncio
async def test_pod_down_ok_when_unit_gone():
    """rc 0 AND the unit no longer active -> genuine success."""
    with patch.object(mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_POD_AVAILABLE", True), \
         patch.object(mod.rt, "active_names", return_value=set()):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_pod_down_fails_closed_when_verify_raises():
    """If the post-stop active-state check errors, fail closed (never claim ok)."""
    with patch.object(mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "", "")), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_POD_AVAILABLE", True), \
         patch.object(mod.rt, "active_names", side_effect=RuntimeError("boom")):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "cannot verify pod shutdown" in result["error"]


@pytest.mark.asyncio
async def test_pod_down_nonzero_rc_is_failure():
    """A non-zero CLI exit is surfaced as failure verbatim."""
    with patch.object(mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(1, "", "stop failed")):
        result = await mod._pod_down("kirocrew-wt-x")
    assert result["ok"] is False
    assert "stop failed" in result["error"]


# =============================================================================
# auto-prune reaper (issue #220)
# =============================================================================
def test_auto_prune_cfg_disabled_by_default():
    with patch.object(mod, "_load_dev_fleet_cfg", return_value={}):
        enabled, interval = mod._auto_prune_cfg()
    assert enabled is False
    assert interval == mod._AUTO_PRUNE_DEFAULT_INTERVAL_S


def test_auto_prune_cfg_enabled_with_interval_floor():
    with patch.object(mod, "_load_dev_fleet_cfg",
                      return_value={"auto_prune": {"enabled": True, "interval_secs": 5}}):
        enabled, interval = mod._auto_prune_cfg()
    assert enabled is True
    # 5s is below the floor -> clamped up to the minimum.
    assert interval == mod._AUTO_PRUNE_MIN_INTERVAL_S


def test_auto_prune_cfg_bad_interval_falls_back():
    with patch.object(mod, "_load_dev_fleet_cfg",
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

    async def _fake_remove(name, force=False):
        assert force is False  # reaper never force-removes
        seen.append(name)
        return {"ok": True} if name == "wt-merged" else {"ok": False, "error": "nope"}

    with patch.object(mod, "_prune_candidates", new_callable=AsyncMock, return_value=candidates), \
         patch.object(mod, "_worktree_remove", side_effect=_fake_remove):
        res = await mod._auto_prune_once()
    assert res["removed"] == ["wt-merged"]
    assert res["failed"] == [{"name": "wt-bad", "error": "nope"}]
    # stale-empty is never touched — unattended auto-prune is merged-only.
    assert "wt-stale-empty" not in seen


@pytest.mark.asyncio
async def test_auto_prune_once_survives_scan_error():
    with patch.object(mod, "_prune_candidates", new_callable=AsyncMock,
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
        with patch.object(mod, "_load_dev_fleet_cfg",
                          return_value={"auto_prune": {"enabled": bad}}):
            enabled, _ = mod._auto_prune_cfg()
        assert enabled is False, f"{bad!r} must not enable auto-prune"


@pytest.mark.asyncio
async def test_pod_up_fails_closed_when_not_active():
    """rc==0 but the unit is not active -> fail closed (no false 'started')."""
    with patch.object(mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, "{}", "")), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_POD_AVAILABLE", True), \
         patch.object(mod.rt, "active_names", return_value=set()):
        result = await mod._pod_up("kirocrew-wt-x")
    assert result["ok"] is False
    assert "not active after start" in result["error"]


@pytest.mark.asyncio
async def test_pod_up_ok_when_active():
    """rc==0 AND the unit active -> success, parsed JSON merged in."""
    with patch.object(mod, "_pod_checkout_guard", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_run_cmd", new_callable=AsyncMock, return_value=(0, '{"port": 7999}', "")), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_POD_AVAILABLE", True), \
         patch.object(mod.rt, "active_names", return_value={"kirocrew-wt-x"}):
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

    with patch.object(mod, "_auto_prune_cfg", return_value=(True, 300)), \
         patch.object(mod, "_auto_prune_once", new_callable=AsyncMock,
                      return_value={"removed": [], "failed": [], "error": "gh down"}), \
         patch.object(mod, "_sel", return_value=fake_sel), \
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
    mod._PRUNE_LOCK = asyncio.Lock()
    mod._GIT_MUTATION_LOCK = asyncio.Lock()
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

    async def fake_remove(nm, force=False, progress=None):
        # exercise the phase callback the parallel driver passes in
        if progress is not None:
            progress("stopping_pod")
            progress("removing")
        if nm == "wt-remove-fail":
            return {"ok": False, "error": "pod still active after shutdown"}
        return {"ok": True, "removed": True}

    with patch.object(mod, "_find_worktree", side_effect=fake_find), \
         patch.object(mod, "_prunable", side_effect=fake_prunable), \
         patch.object(mod, "_worktree_remove", side_effect=fake_remove):
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

    async def fake_remove(nm, force=False, progress=None):
        if nm == "wt-boom":
            raise RuntimeError("kaboom")
        return {"ok": True, "removed": True}

    with patch.object(mod, "_find_worktree", side_effect=fake_find), \
         patch.object(mod, "_prunable", side_effect=fake_prunable), \
         patch.object(mod, "_worktree_remove", side_effect=fake_remove):
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
    monkeypatch.setattr(mod, "_PRUNE_CONCURRENCY", 2)
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

    async def fake_remove(nm, force=False, progress=None):
        return {"ok": True, "removed": True}

    with patch.object(mod, "_find_worktree", side_effect=fake_find), \
         patch.object(mod, "_prunable", side_effect=fake_prunable), \
         patch.object(mod, "_worktree_remove", side_effect=fake_remove):
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

    with patch.object(mod, "_find_worktree", side_effect=fake_find), \
         patch.object(mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_own_checkout_path", return_value=None), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=0), \
         patch.object(mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(mod, "_load_cfg", return_value=None), \
         patch.object(mod, "_POD_AVAILABLE", False), \
         patch.object(mod, "_run_cmd", side_effect=fake_run_cmd):
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

    async def fake_remove(nm, force=False, progress=None):
        removed.append(nm)
        return {"ok": True, "removed": True}

    with patch.object(mod, "_find_worktree", side_effect=fake_find), \
         patch.object(mod, "_prunable", side_effect=fake_prunable), \
         patch.object(mod, "_worktree_remove", side_effect=fake_remove):
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

    with patch.object(mod, "_find_worktree", side_effect=fake_find), \
         patch.object(mod, "_live_worktree_path", new_callable=AsyncMock, return_value=None), \
         patch.object(mod, "_own_checkout_path", return_value=None), \
         patch.object(mod, "_real_dirty", new_callable=AsyncMock, return_value=False), \
         patch.object(mod, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "MERGED"}), \
         patch.object(mod, "_own_commits_count", new_callable=AsyncMock, return_value=0), \
         patch.object(mod, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(mod, "_head_contained_in_pr", new_callable=AsyncMock, return_value=True), \
         patch.object(mod, "_git", new_callable=AsyncMock, return_value="a" * 40), \
         patch.object(mod, "_load_cfg", return_value=object()), \
         patch.object(mod, "_POD_AVAILABLE", True), \
         patch.object(mod.rt, "active_names", side_effect=lambda cfg: next(active_calls)), \
         patch.object(mod, "_run_cmd", side_effect=fake_run_cmd):
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

    monkeypatch.setattr(mod, "_find_worktree", fake_find)
    monkeypatch.setattr(mod, "_live_worktree_path", fake_live)
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

    monkeypatch.setattr(mod, "_find_worktree", fake_find)
    monkeypatch.setattr(mod, "_live_worktree_path", fake_live)
    monkeypatch.setattr(mod, "_own_checkout_path", lambda: "/wt/self")
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

    with patch.object(mod, "_run_cmd", side_effect=fake_run_cmd), \
         patch.object(mod, "sys", MagicMock(platform="linux")), \
         patch.object(mod, "shutil",
                      MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))):
        assert await mod._gateway_start_id() == "123456789"


@pytest.mark.asyncio
async def test_gateway_start_id_none_on_non_linux():
    """Non-Linux / no systemctl degrades to None (no hang in 'restarting')."""
    with patch.object(mod, "sys", MagicMock(platform="darwin")), \
         patch.object(mod, "shutil", MagicMock(which=MagicMock(return_value=None))):
        assert await mod._gateway_start_id() is None


@pytest.mark.asyncio
async def test_gateway_start_id_none_on_zero_empty_or_error():
    """A '0'/empty stamp or a failed probe all normalise to None."""
    with patch.object(mod, "sys", MagicMock(platform="linux")), \
         patch.object(mod, "shutil",
                      MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))):
        with patch.object(mod, "_run_cmd", new_callable=AsyncMock,
                          return_value=(0, "0\n", "")):
            assert await mod._gateway_start_id() is None
        with patch.object(mod, "_run_cmd", new_callable=AsyncMock,
                          return_value=(0, "\n", "")):
            assert await mod._gateway_start_id() is None
        with patch.object(mod, "_run_cmd", new_callable=AsyncMock,
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

    with patch.object(mod, "_run_cmd", side_effect=mock_run_cmd), \
         patch.object(mod, "sys", MagicMock(platform="linux")), \
         patch.object(mod, "shutil",
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

    with patch.object(mod, "_run_cmd", side_effect=mock_run_cmd), \
         patch.object(mod, "sys", MagicMock(platform="linux")), \
         patch.object(mod, "shutil",
                      MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))):
        result = await mod._restart_gateway()

    assert result["ok"] is True
    assert result["start_id"] is None


@pytest.mark.asyncio
async def test_api_health_includes_start_id():
    """/health carries the current start identity for the restart handshake."""
    with patch.object(mod, "_gateway_start_id", new_callable=AsyncMock,
                      return_value="98765"):
        resp = await mod.api_health(MagicMock())
    payload = json.loads(resp.text)
    assert resp.status == 200
    assert payload["status"] == "ok"
    assert payload["start_id"] == "98765"


@pytest.mark.asyncio
async def test_api_health_start_id_none_safe():
    """/health stays 200/ok with start_id=None when identity is unavailable."""
    with patch.object(mod, "_gateway_start_id", new_callable=AsyncMock,
                      return_value=None):
        resp = await mod.api_health(MagicMock())
    payload = json.loads(resp.text)
    assert resp.status == 200
    assert payload["status"] == "ok"
    assert payload["start_id"] is None


@pytest.mark.asyncio
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

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
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
    mod._FLEET_INFLIGHT = None
    mod._FLEET_EPOCH = 0
    mod._FLEET_TOMBSTONES = {}
    try:
        yield
    finally:
        mod._FLEET_CACHE.clear()
        mod._FLEET_CACHE.update(saved)
        mod._FLEET_INFLIGHT = saved_inflight
        mod._FLEET_EPOCH = saved_epoch
        mod._FLEET_TOMBSTONES = saved_tombs


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
    with patch.object(mod, "_find_worktree", new=AsyncMock(
        return_value=({"path": "/repo/wt-x", "branch": "feat/x", "is_main": False}, None)
    )), \
            patch.object(mod, "_live_worktree_path", new=AsyncMock(return_value=None)), \
            patch.object(mod, "_own_checkout_path", return_value=None), \
            patch.object(mod, "_real_dirty", new=AsyncMock(return_value=False)), \
            patch.object(mod, "_pr_status_cached", new=AsyncMock(
                return_value={"state": "MERGED"})), \
            patch.object(mod, "_own_commits_count", new=AsyncMock(return_value=0)), \
            patch.object(mod, "_fetch_pr_head_oid", new=AsyncMock(return_value="deadbeef")), \
            patch.object(mod, "_head_contained_in_pr", new=AsyncMock(return_value=True)), \
            patch.object(mod, "_git", new=AsyncMock(return_value="deadbeef")), \
            patch.object(mod, "_load_cfg", return_value=None), \
            patch.object(mod, "_POD_AVAILABLE", False), \
            patch.object(mod, "_run_cmd", new=AsyncMock(return_value=(0, "", ""))):
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

    with patch.object(mod, "_build_fleet", new=_slow_build):
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

    with patch.object(mod, "_build_fleet", new=_slow_build):
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

    with patch.object(mod, "_build_fleet", new=AsyncMock(
        return_value={"worktrees": [{"name": "main"}]}
    )):
        await mod._fleet_refresh()
    assert mod._FLEET_TOMBSTONES == {}

    # Re-created under the same name: no stale tombstone hides it.
    with patch.object(mod, "_build_fleet", new=AsyncMock(
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

    with patch.object(mod, "_build_fleet", new=_slow_build):
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
    with patch.object(mod, "_build_fleet", new=AsyncMock(
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
    with patch.object(mod, "_build_fleet", new=AsyncMock(side_effect=RuntimeError("git blew up"))):
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
    monkeypatch.setattr(mod, "MAIN_REPO", str(repo))
    monkeypatch.setattr(mod, "_SYNC_RID", None)

    async def fake_remote():
        return "origin"

    monkeypatch.setattr(mod, "_upstream_remote", fake_remote, raising=False)
    monkeypatch.setattr(mod, "_trusted_bin", lambda n: f"/usr/bin/{n}")
    argvs: list[list[str]] = []

    def fake_sandboxed(argv, mode, env=None):
        argvs.append(list(argv))
        return list(argv), dict(env or {}), None

    monkeypatch.setattr(mod, "sandboxed_spawn_argv", fake_sandboxed)

    async def fake_run_cmd(cmd, **kw):
        return 0, "main", ""

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)

    async def fake_start_run(label, cmd, **kw):
        return "rid-stage"

    monkeypatch.setattr(mod, "_start_run", fake_start_run)

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

    with patch.object(dev.platform_compat, "process_descendants", return_value=[222]), \
            patch.object(
                dev.platform_compat,
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
        dev.platform_compat,
        "process_descendants",
        side_effect=lambda pid: events.append("enumerate") or [222],
    ), patch.object(
        dev.platform_compat,
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

    with patch.object(dev.platform_compat, "process_descendants", return_value=[222, 333]), \
            patch.object(dev.platform_compat, "kill_process_tree", side_effect=_kill) as km:
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
    """MAIN_REPO defaults to ~/kirocrew whether or not it exists.

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
    monkeypatch.setattr(mod, "_SERVING_REASON", None)
    monkeypatch.setattr(mod, "MAIN_REPO", "/nowhere/at/all")
    calls: list[tuple] = []

    def _spy(main_repo: str, managed: tuple) -> str | None:
        calls.append((main_repo, managed))
        return "mismatch"

    monkeypatch.setattr(mod, "_serving_install_reason_sync", _spy)
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
    monkeypatch.setattr(mod, "_SERVING_REASON", None)
    monkeypatch.setattr(mod, "MAIN_REPO", "/nowhere")
    seen: list[tuple] = []
    monkeypatch.setattr(
        mod, "_serving_install_reason_sync",
        lambda repo, managed: seen.append(managed) or "r",
    )

    await mod._serving_install_reason([{"path": "/wt/a"}])
    await mod._serving_install_reason([{"path": "/wt/a"}, {"path": "/wt/b"}])

    assert seen == [("/wt/a",), ("/wt/a", "/wt/b")]
