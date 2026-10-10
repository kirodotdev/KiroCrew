"""A Dev Fleet worktree removal and a pod start on that worktree exclude each other.

Real: ``worktree_ops._worktree_remove`` and ``worktree_ops._pod_up``, a scratch git
repository under ``tmp_path`` with real worktrees, and a real ``git worktree remove``.
Stand-ins: the pod backend (``runtime.rt``, an in-memory set of active pod names) and
the ``kirocrew pod up`` CLI, which marks the pod active the way its unit does.
"""

import asyncio
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.apps.builtins.dev_fleet import fleet_state, live, repository, runtime, worktree_ops

NAME = "feature-wt"
OTHER = "other-wt"
BOUND = 10.0


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class _PodError(Exception):
    pass


class _Absent(_PodError):
    pass


class _Lease:
    refusal = None

    def __bool__(self) -> bool:
        return True


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=repo,
    )
    trees = {name: tmp_path / name for name in (NAME, OTHER)}
    for name, path in trees.items():
        _git("worktree", "add", "-q", "-b", f"feat/{name}", str(path), cwd=repo)

    active: set[str] = set()
    cfg = SimpleNamespace(pod_root=tmp_path / "pods")
    cfg.pod_root.mkdir()
    rt = SimpleNamespace(
        PodError=_PodError,
        PodBackendAbsent=_Absent,
        PodOwnershipUnproven=_PodError,
        require_backend=lambda: None,
        active_names=lambda c: set(active),
        orphan_homes=lambda c, *a, **k: [],
        pod_home=lambda c, n: cfg.pod_root / n,
    )

    async def find(name):
        path = trees.get(name)
        if path is not None and path.is_dir():
            return {"path": str(path), "branch": f"feat/{name}", "is_main": False}, None
        return None, f"unknown worktree: {name!r}"

    @asynccontextmanager
    async def removal_lease(path):
        yield _Lease()

    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", [])
    monkeypatch.setattr(fleet_state, "_OWNER_REPO", None)
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(live, "_LIVE_WORKTREE", None)
    monkeypatch.setattr(live, "_LIVE_CHECK_AT", 0.0)
    monkeypatch.setattr(live, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", asyncio.Lock())
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    monkeypatch.setattr(worktree_ops, "_GIT_MUTATION_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(runtime, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(runtime, "rt", rt)
    monkeypatch.setattr(repository, "MAIN_REPO", str(repo))
    monkeypatch.setattr(repository, "_repo", lambda: str(repo))
    monkeypatch.setattr(repository, "_find_worktree", find)
    monkeypatch.setattr(live, "_live_worktree_path", AsyncMock(return_value=None))
    monkeypatch.setattr(live, "_staged_target", lambda: None)
    monkeypatch.setattr(live, "_own_checkout_path", lambda: None)
    monkeypatch.setattr(live, "removal_lease", removal_lease)
    monkeypatch.setattr(live, "removal_lease_lost", lambda path: False)
    monkeypatch.setattr(live, "confirm_removal_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(fleet_state, "_is_pr_merged", lambda pr: False)
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="abc1234"))
    monkeypatch.setattr(fleet_state, "_fleet_forget", lambda name: None)
    monkeypatch.setattr(
        runtime, "_sel", lambda: type("_S", (), {"log_tool_invocation": lambda self, **kw: None})()
    )
    monkeypatch.setattr(runtime, "_warm_build_path", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_find_cli", lambda: ["kirocrew"])
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", lambda c, n: (False, None))
    monkeypatch.setattr(worktree_ops, "_mint_pod_token_locked", lambda c, n, p: {"ok": True})

    world = SimpleNamespace(
        monkeypatch=monkeypatch,
        trees=trees,
        active=active,
        # The pod CLI stand-in awaits this before the unit goes active.
        pod_cli=AsyncMock(return_value=None),
    )

    async def run_cmd(cmd, cwd=None, env=None, timeout=None, pre_spawn=None, **kw):
        if cmd[:3] == ["kirocrew", "pod", "up"]:
            await world.pod_cli(cmd[3])
            active.add(cmd[3])
            return 0, '{"base_url": "http://127.0.0.1:1"}', ""
        if pre_spawn is not None:
            refused = await pre_spawn()
            if refused:
                return 1, "", refused
        # A real child never inherits the test process's cwd: the removal passes
        # none (git is pointed at the repo with ``-C``), so it runs under tmp_path.
        child_cwd = tmp_path if cwd is None else Path(cwd)
        assert child_cwd.resolve().is_relative_to(tmp_path.resolve()), child_cwd
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(child_cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)
    return world


async def _removal_held_at_its_git_spawn(world):
    """Start a removal of NAME and hold it at the lease renewal right before git."""
    at_gate, release = asyncio.Event(), asyncio.Event()

    async def confirm(path):
        at_gate.set()
        await asyncio.wait_for(release.wait(), BOUND)
        return True

    world.monkeypatch.setattr(live, "confirm_removal_lease", confirm)
    removal = asyncio.ensure_future(worktree_ops._worktree_remove(NAME))
    await asyncio.wait_for(at_gate.wait(), BOUND)
    return removal, release


@pytest.mark.asyncio
async def test_a_removal_issued_during_a_pod_start_does_not_delete_the_running_pods_checkout(world):
    cli_running, unit_may_start = asyncio.Event(), asyncio.Event()

    async def pod_cli(name):
        cli_running.set()
        await asyncio.wait_for(unit_may_start.wait(), BOUND)

    async def confirm(path):
        # The unit goes active while the removal is between its last pod check
        # and `git worktree remove`.
        unit_may_start.set()
        return True

    world.pod_cli = pod_cli
    world.monkeypatch.setattr(live, "confirm_removal_lease", confirm)
    pod_up = asyncio.ensure_future(worktree_ops._pod_up(NAME))
    await asyncio.wait_for(cli_running.wait(), BOUND)
    removed = await worktree_ops._worktree_remove(NAME)
    unit_may_start.set()
    started = await asyncio.wait_for(pod_up, BOUND)

    assert started["ok"] is True, started
    assert NAME in world.active
    assert removed["ok"] is False, removed
    assert world.trees[NAME].is_dir(), "the running pod's checkout was deleted"


@pytest.mark.asyncio
async def test_a_pod_start_during_a_removal_is_refused(world):
    removal, release = await _removal_held_at_its_git_spawn(world)
    started = await worktree_ops._pod_up(NAME)
    release.set()
    removed = await asyncio.wait_for(removal, BOUND)

    assert started["ok"] is False, started
    assert NAME not in world.active
    assert removed["ok"] is True, removed
    assert not world.trees[NAME].exists()


@pytest.mark.asyncio
async def test_control_a_pod_started_before_the_last_check_still_refuses_the_removal(world):
    def lost(path):
        world.active.add(NAME)
        return False

    world.monkeypatch.setattr(live, "removal_lease_lost", lost)
    removed = await worktree_ops._worktree_remove(NAME)

    assert removed == {"ok": False, "error": "pod became active again before removal — refusing"}
    assert world.trees[NAME].is_dir()


@pytest.mark.asyncio
async def test_control_a_pod_start_on_another_worktree_is_not_blocked(world):
    removal, release = await _removal_held_at_its_git_spawn(world)
    started = await asyncio.wait_for(worktree_ops._pod_up(OTHER), BOUND)
    release.set()
    removed = await asyncio.wait_for(removal, BOUND)

    assert started["ok"] is True, started
    assert world.active == {OTHER}
    assert removed["ok"] is True, removed
    assert world.trees[OTHER].is_dir()


@pytest.mark.asyncio
async def test_control_an_idle_worktree_is_removed_as_before(world):
    removed = await worktree_ops._worktree_remove(NAME)

    assert removed["ok"] is True, removed
    assert removed["stopped_pod"] is False
    assert not world.trees[NAME].exists()
    assert world.trees[OTHER].is_dir()


@pytest.mark.asyncio
async def test_a_pod_start_for_an_unknown_worktree_keeps_no_lock_row(world):
    """``_wt_lock`` rows are never removed, so a name with no worktree must not get one."""
    result = await worktree_ops._pod_up("no-such-worktree")
    assert result["ok"] is False and "unknown worktree" in result["error"]
    assert (
        "no-such-worktree" not in worktree_ops._WT_LOCKS
    ), "a pod start for a name with no worktree left a permanent lock row"
