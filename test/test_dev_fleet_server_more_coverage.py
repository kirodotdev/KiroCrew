"""Further coverage for the Dev Fleet standalone backend.

Complements ``test_dev_fleet_server_coverage.py`` by driving the branches that
need a *spawn* double rather than a plain helper stub: the sandboxed
``_run_cmd`` chokepoint, the ``_start_run`` streaming worker, the gh/git PR
query layer, trusted-binary resolution, live-checkout resolution, and the
restart-gateway backends.

No real subprocess, no git, no gh, no network, no sleeps beyond a single 10 ms
``wait_for`` deadline, and no writes outside ``tmp_path``. Every spawn is
intercepted at BOTH chokepoints the product uses -- ``sandboxed_spawn_argv``
(which raises on a runner with no sandbox backend) and
``create_subprocess_limited`` -- so nothing here depends on the host having a
sandbox, a service manager, or POSIX process groups.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    gateway_service,
    http_api,
    live,
    repository,
    runtime,
    worktree_ops,
)


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------
class _FakeProc:
    """Subprocess stand-in for the two spawn call sites in this module."""

    def __init__(
        self,
        *,
        lines: list[bytes] | None = None,
        rc: int = 0,
        readline_error: BaseException | None = None,
        communicate_delay: float | None = None,
        kill_error: BaseException | None = None,
    ) -> None:
        self.pid = 4321
        self.returncode: int | None = None
        self.stdout = self
        self.kills = 0
        self.waits = 0
        self.communicates = 0
        self._lines = list(lines or [])
        self._rc = rc
        self._readline_error = readline_error
        self._communicate_delay = communicate_delay
        self._kill_error = kill_error

    # -- stream side (used by _start_run) --
    async def readline(self) -> bytes:
        if self._readline_error is not None:
            raise self._readline_error
        return self._lines.pop(0) if self._lines else b""

    # -- communicate side (used by _run_cmd) --
    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicates += 1
        # A killed child's pipes close: the post-kill reap returns promptly
        # rather than re-serving the hang that triggered the timeout.
        if self._communicate_delay is not None and self.kills == 0:
            await asyncio.sleep(self._communicate_delay)
        self.returncode = self._rc
        return b"out", b"err"

    async def wait(self) -> int:
        self.waits += 1
        self.returncode = self._rc
        return self._rc

    def kill(self) -> None:
        self.kills += 1
        if self._kill_error is not None:
            raise self._kill_error


def _spawn_returns(monkeypatch, proc: _FakeProc) -> list[tuple]:
    """Install a ``create_subprocess_limited`` double; return the call log."""
    calls: list[tuple] = []

    async def _fake(*argv, **kwargs):
        calls.append((argv, kwargs))
        return proc

    monkeypatch.setattr(runtime, "create_subprocess_limited", _fake)
    return calls


def _spawn_raises(monkeypatch, exc: BaseException) -> None:
    async def _fake(*argv, **kwargs):
        raise exc

    monkeypatch.setattr(runtime, "create_subprocess_limited", _fake)


def _passthrough_sandbox(monkeypatch, cleanup: str | None = None) -> None:
    """``sandboxed_spawn_argv`` double: identity argv, no OS isolation."""
    monkeypatch.setattr(
        runtime,
        "sandboxed_spawn_argv",
        lambda argv, tier, env=None: (list(argv), dict(env or {}), cleanup),
    )


def _run_cmd_queue(monkeypatch, results: list[tuple[int, str, str]]) -> list[list[str]]:
    """Replace ``_run_cmd`` with a scripted queue; return the argv log."""
    seen: list[list[str]] = []

    async def _fake(cmd, **kwargs):
        seen.append(list(cmd))
        return results[len(seen) - 1] if len(seen) <= len(results) else (1, "", "")

    monkeypatch.setattr(runtime, "_run_cmd", _fake)
    return seen


async def _drain_run(rid: str) -> dict:
    """Let the _start_run worker task finish, then return its record."""
    for _ in range(2000):
        if runtime._RUNS[rid]["status"] != "running":
            return runtime._RUNS[rid]
        await asyncio.sleep(0)
    raise AssertionError(f"run {rid} never left 'running'")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset the module caches these tests read or write."""
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", [])
    monkeypatch.setattr(fleet_state, "_OWNER_REPO", None)
    monkeypatch.setattr(fleet_state, "_OWNER_REPO_RETRY_AT", 0.0)
    monkeypatch.setattr(runtime, "_TRUSTED_BIN_CACHE", {})
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", None)
    monkeypatch.setattr(live, "_LIVE_WORKTREE", None)
    monkeypatch.setattr(live, "_LIVE_CHECK_AT", 0.0)
    monkeypatch.setattr(live, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", asyncio.Lock())
    # Shutdown admission state: each test starts with a clean (non-shutdown) process.
    monkeypatch.setattr(runtime, "_SHUTDOWN_IN_PROGRESS", False)
    monkeypatch.setattr(runtime, "_SHUTDOWN_ADMISSION_LOCK", asyncio.Lock())


# --------------------------------------------------------------------------
# _run_cmd -- the sandboxed spawn chokepoint
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_cmd_refuses_unresolvable_bare_tool(monkeypatch):
    """A bare command name that resolves to no trusted binary never spawns."""
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: None)
    _spawn_raises(monkeypatch, AssertionError("must not spawn"))

    rc, out, err = await runtime._run_cmd(["git", "status"])

    assert (rc, out) == (-1, "")
    assert err.startswith(runtime._UNRESOLVED_TOOL_PREFIX)
    assert "'git'" in err


@pytest.mark.asyncio
async def test_run_cmd_spawn_oserror_deletes_sandbox_cleanup_file(monkeypatch, tmp_path):
    """An OSError from the spawn reports it AND removes the launcher temp file."""
    leftover = tmp_path / "launcher.sh"
    leftover.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/git")
    _passthrough_sandbox(monkeypatch, cleanup=str(leftover))
    _spawn_raises(monkeypatch, OSError("ENOMEM"))

    rc, out, err = await runtime._run_cmd(["git", "status"])

    assert (rc, out) == (-1, "")
    assert err == "spawn failed: ENOMEM"
    assert not leftover.exists()


@pytest.mark.asyncio
async def test_run_cmd_timeout_kills_tree_and_reports(monkeypatch, tmp_path):
    """A communicate() that outlives *timeout* is reaped, not left running."""
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/git")
    _passthrough_sandbox(monkeypatch, cleanup=str(tmp_path / "never-written"))
    proc = _FakeProc(communicate_delay=5.0)
    _spawn_returns(monkeypatch, proc)
    killed: list[int] = []
    monkeypatch.setattr(runtime, "_kill_tree", AsyncMock(side_effect=killed.append))
    # The shared reap helper also signals the tree; intercept it so a fake pid
    # never reaches a real killpg on the host.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", AsyncMock())

    rc, out, err = await runtime._run_cmd(["git", "status"], timeout=0)

    assert (rc, out, err) == (-1, "", "timeout (0s)")
    assert killed == [proc.pid]
    # The reap drains pipes via communicate() after kill(); a bare wait() on
    # a killed child blocked writing into a full pipe would hang the caller
    # forever (#5989). With timeout=0 the site's own communicate() is
    # cancelled before it ever runs, so the single recorded call IS the reap.
    # The missing cleanup file was tolerated.
    assert (proc.kills, proc.communicates, proc.waits) == (1, 1, 0)


@pytest.mark.asyncio
async def test_run_cmd_success_removes_cleanup_file(monkeypatch, tmp_path):
    """The happy path deletes the sandbox launcher in its finally block."""
    launcher = tmp_path / "launcher2.sh"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/git")
    _passthrough_sandbox(monkeypatch, cleanup=str(launcher))
    _spawn_returns(monkeypatch, _FakeProc(rc=0))

    rc, out, err = await runtime._run_cmd(["git", "status"])

    assert (rc, out, err) == (0, "out", "err")
    assert not launcher.exists()


@pytest.mark.asyncio
async def test_kill_tree_swallows_process_lookup_error(monkeypatch):
    """A pid that vanished between enumeration and signalling is not an error."""
    def _boom(pid: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(runtime, "_kill_tree_sync", _boom)
    assert await runtime._kill_tree(999999) is None


# --------------------------------------------------------------------------
# _start_run -- background streaming worker
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_run_records_spawn_failure(monkeypatch):
    _spawn_raises(monkeypatch, OSError("no fork"))

    rid = await runtime._start_run("provision", ["kirocrew", "pod", "up"])
    rec = await _drain_run(rid)

    assert rec["exit_code"] == -1
    assert rec["output"] == ["[error] spawn failed: no fork"]
    assert rec["label"] == "provision"


@pytest.mark.asyncio
async def test_start_run_parses_step_markers_and_caps_output(monkeypatch, tmp_path):
    """Markers set step/step_label; the tail window is capped at 500 lines."""
    done = tmp_path / "profile.json"
    done.write_text("{}", encoding="utf-8", newline="\n")
    lines = [b"::step::2::npm ci\n", b"::step::nope::\n"]
    lines += [b"line %d\n" % i for i in range(510)]
    _spawn_returns(monkeypatch, _FakeProc(lines=lines, rc=0))

    rid = await runtime._start_run(
        "build",
        ["kirocrew", "pod", "provision"],
        cleanup_paths=[str(done), str(tmp_path / "absent")],
    )
    rec = await _drain_run(rid)

    assert (rec["status"], rec["exit_code"]) == ("done", 0)
    # The malformed marker leaves both fields at the last good values.
    assert (rec["step"], rec["step_label"]) == (2, "npm ci")
    assert len(rec["output"]) == 500
    assert rec["output"][-1] == "line 509"
    # A missing cleanup path is tolerated; a real one is deleted.
    assert not done.exists()


@pytest.mark.asyncio
async def test_start_run_deadline_marks_timeout(monkeypatch):
    """A run past _RUN_DEADLINE_S is killed and recorded as timeout, not done."""
    monkeypatch.setattr(runtime, "_RUN_DEADLINE_S", -1)
    proc = _FakeProc(lines=[b"never read\n"], rc=0)
    _spawn_returns(monkeypatch, proc)
    killed: list[int] = []
    monkeypatch.setattr(runtime, "_kill_tree", AsyncMock(side_effect=killed.append))

    rid = await runtime._start_run("sync", ["kirocrew", "sync"])
    rec = await _drain_run(rid)

    assert rec["status"] == "timeout"
    assert rec["exit_code"] == -1
    assert rec["output"] == ["[timeout] process killed after -1s deadline"]
    assert killed == [proc.pid]


@pytest.mark.asyncio
async def test_start_run_stream_error_reaps_live_child(monkeypatch):
    """A readline() blowup still reaps the subprocess before recording."""
    proc = _FakeProc(readline_error=ValueError("line too long"))
    _spawn_returns(monkeypatch, proc)
    killed: list[int] = []
    monkeypatch.setattr(runtime, "_kill_tree", AsyncMock(side_effect=killed.append))
    # The shared reap helper also signals the tree; intercept it so the fake
    # pid never reaches a real killpg on the host.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", AsyncMock())

    rid = await runtime._start_run("sync", ["kirocrew", "sync"])
    rec = await _drain_run(rid)

    assert rec["status"] == "done"
    assert rec["exit_code"] == -1
    assert rec["output"] == ["[error] line too long"]
    assert killed == [proc.pid]
    assert proc.kills == 1


@pytest.mark.asyncio
async def test_start_run_stream_error_tolerates_already_reaped_child(monkeypatch):
    """kill() raising ProcessLookupError must not mask the original error."""
    proc = _FakeProc(readline_error=ValueError("boom"), kill_error=ProcessLookupError(4321))
    _spawn_returns(monkeypatch, proc)
    monkeypatch.setattr(runtime, "_kill_tree", AsyncMock())
    # The shared reap helper also signals the tree; intercept it so the fake
    # pid never reaches a real killpg on the host.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", AsyncMock())

    rid = await runtime._start_run("sync", ["kirocrew", "sync"])
    rec = await _drain_run(rid)

    assert rec["output"] == ["[error] boom"]


# --------------------------------------------------------------------------
# PR query layer (gh / git through _run_cmd)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_repo_owner_name_none_when_remote_lookup_fails(monkeypatch):
    monkeypatch.setattr(repository, "_upstream_remote", AsyncMock(return_value="origin"))
    _run_cmd_queue(monkeypatch, [(1, "", "fatal: no such remote")])
    assert await fleet_state._repo_owner_name() is None


@pytest.mark.asyncio
async def test_repo_owner_name_none_when_url_unparseable(monkeypatch):
    monkeypatch.setattr(repository, "_upstream_remote", AsyncMock(return_value="origin"))
    _run_cmd_queue(monkeypatch, [(0, "not-a-remote-url\n", "")])
    assert await fleet_state._repo_owner_name() is None


@pytest.mark.asyncio
async def test_repo_owner_name_parses_ssh_url(monkeypatch):
    monkeypatch.setattr(repository, "_upstream_remote", AsyncMock(return_value="origin"))
    _run_cmd_queue(monkeypatch, [(0, "git@github.com:kirodotdev/KiroCrew.git\n", "")])
    assert await fleet_state._repo_owner_name() == "kirodotdev/KiroCrew"


@pytest.mark.asyncio
async def test_get_owner_repo_caches_success_and_backs_off_failure(monkeypatch):
    """Success is cached permanently; a failure arms a retry deadline."""
    calls = []

    async def _lookup():
        calls.append(1)
        return None

    monkeypatch.setattr(fleet_state, "_repo_owner_name", _lookup)
    assert await fleet_state._get_owner_repo() is None
    assert fleet_state._OWNER_REPO_RETRY_AT > 0
    # Second call is inside the back-off window: no new lookup.
    assert await fleet_state._get_owner_repo() is None
    assert len(calls) == 1

    monkeypatch.setattr(fleet_state, "_OWNER_REPO_RETRY_AT", 0.0)
    monkeypatch.setattr(fleet_state, "_repo_owner_name", AsyncMock(return_value="o/r"))
    assert await fleet_state._get_owner_repo() == "o/r"
    # Now cached: a lookup that would raise is never reached.
    monkeypatch.setattr(fleet_state, "_repo_owner_name", AsyncMock(side_effect=RuntimeError))
    assert await fleet_state._get_owner_repo() == "o/r"


@pytest.mark.asyncio
async def test_pr_query_one_none_on_gh_failure(monkeypatch):
    _run_cmd_queue(monkeypatch, [(1, "", "gh: not logged in")])
    assert await fleet_state._pr_query_one("o/r", "feat/x") is None


@pytest.mark.asyncio
async def test_pr_query_one_none_on_unparseable_json(monkeypatch):
    _run_cmd_queue(monkeypatch, [(0, "<html>rate limited</html>", "")])
    assert await fleet_state._pr_query_one("o/r", "feat/x") is None


@pytest.mark.asyncio
async def test_pr_query_one_none_on_empty_result(monkeypatch):
    _run_cmd_queue(monkeypatch, [(0, "[]", "")])
    assert await fleet_state._pr_query_one("o/r", "feat/x") is None


@pytest.mark.asyncio
async def test_pr_query_one_moves_body_to_internal_key(monkeypatch):
    """Body and head identity become internal fields omitted from the payload."""
    payload = json.dumps([{"number": 7, "state": "OPEN", "body": None, "headRefOid": "a" * 40}])
    seen = _run_cmd_queue(monkeypatch, [(0, payload, "")])

    pr = await fleet_state._pr_query_one("o/r", "feat/x")

    assert pr is not None
    assert pr["_repo"] == "o/r"
    assert pr["_body"] == ""
    assert pr["_head_oid"] == "a" * 40
    assert "body" not in pr
    assert "headRefOid" not in pr
    assert "--head" in seen[0] and "feat/x" in seen[0]


@pytest.mark.asyncio
async def test_fetch_pr_status_needs_owner_and_branch(monkeypatch):
    monkeypatch.setattr(fleet_state, "_get_owner_repo", AsyncMock(return_value=None))
    assert await fleet_state._fetch_pr_status("feat/x") is None

    monkeypatch.setattr(fleet_state, "_get_owner_repo", AsyncMock(return_value="o/r"))
    assert await fleet_state._fetch_pr_status("") is None


@pytest.mark.asyncio
async def test_fetch_pr_status_falls_back_to_legacy_remote(monkeypatch):
    """A miss upstream is retried against the ancestor-verified legacy repos."""
    monkeypatch.setattr(fleet_state, "_get_owner_repo", AsyncMock(return_value="new/repo"))
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", ["dead/repo", "old/repo"])
    asked: list[str] = []

    async def _one(owner_repo: str, branch: str):
        asked.append(owner_repo)
        return {"number": 1, "_repo": owner_repo} if owner_repo == "old/repo" else None

    monkeypatch.setattr(fleet_state, "_pr_query_one", _one)

    pr = await fleet_state._fetch_pr_status("feat/x")

    assert pr == {"number": 1, "_repo": "old/repo"}
    assert asked == ["new/repo", "dead/repo", "old/repo"]


@pytest.mark.asyncio
async def test_fetch_pr_status_none_when_no_repo_has_the_branch(monkeypatch):
    monkeypatch.setattr(fleet_state, "_get_owner_repo", AsyncMock(return_value="new/repo"))
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", ["old/repo"])
    monkeypatch.setattr(fleet_state, "_pr_query_one", AsyncMock(return_value=None))
    assert await fleet_state._fetch_pr_status("feat/x") is None


@pytest.mark.asyncio
async def test_head_contained_in_pr_identical_oid_skips_git(monkeypatch):
    _run_cmd_queue(monkeypatch, [])  # any spawn would return (1, "", "")
    assert await fleet_state._head_contained_in_pr("/wt", " abc123 ", "abc123\n") is True


@pytest.mark.asyncio
async def test_head_contained_in_pr_uses_ancestor_check(monkeypatch):
    seen = _run_cmd_queue(monkeypatch, [(0, "", "")])
    assert await fleet_state._head_contained_in_pr("/wt", "aaa", "bbb") is True
    assert seen[0][-3:] == ["--is-ancestor", "aaa", "bbb"]


@pytest.mark.asyncio
async def test_head_contained_in_pr_false_when_diverged(monkeypatch):
    _run_cmd_queue(monkeypatch, [(1, "", "")])
    assert await fleet_state._head_contained_in_pr("/wt", "aaa", "bbb") is False


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_requires_owner_and_branch(monkeypatch):
    monkeypatch.setattr(fleet_state, "_get_owner_repo", AsyncMock(return_value=None))
    assert await fleet_state._fetch_pr_head_oid("feat/x") is None
    assert await fleet_state._fetch_pr_head_oid("", repo="o/r") is None


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_none_when_gh_fails(monkeypatch):
    _run_cmd_queue(monkeypatch, [(1, "", "gh error")])
    assert await fleet_state._fetch_pr_head_oid("feat/x", repo="o/r") is None


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_none_on_bad_json(monkeypatch):
    _run_cmd_queue(monkeypatch, [(0, "not json", "")])
    assert await fleet_state._fetch_pr_head_oid("feat/x", repo="o/r") is None


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_gated_on_merged_state(monkeypatch):
    """An OPEN PR yields None: only a MERGED verdict may authorize removal."""
    _run_cmd_queue(
        monkeypatch,
        [
            (0, json.dumps({"state": "OPEN", "headRefOid": "deadbeef"}), ""),
            (0, json.dumps({"state": "MERGED", "headRefOid": "cafe1234"}), ""),
        ],
    )
    assert await fleet_state._fetch_pr_head_oid("feat/x", repo="o/r") is None
    assert await fleet_state._fetch_pr_head_oid("feat/x", repo="o/r") == "cafe1234"


@pytest.mark.asyncio
async def test_pr_status_cached_serves_terminal_entry_without_refetch(monkeypatch):
    """A MERGED entry is permanently terminal; a stale OPEN entry refetches."""
    monkeypatch.setattr(
        fleet_state,
        "_PR_CACHE",
        {
            "merged": {"data": {"state": "MERGED"}, "ts": 0.0},
            "stale": {"data": {"state": "OPEN"}, "ts": 0.0},
        },
    )
    fetch = AsyncMock(return_value={"state": "OPEN", "number": 9})
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)

    assert await fleet_state._pr_status_cached("merged") == {"state": "MERGED"}
    fetch.assert_not_awaited()

    assert await fleet_state._pr_status_cached("stale") == {"state": "OPEN", "number": 9}
    assert fleet_state._PR_CACHE["stale"]["data"]["number"] == 9


@pytest.mark.asyncio
async def test_pr_status_cached_skips_base_branch(monkeypatch):
    monkeypatch.setattr(fleet_state, "_fetch_pr_status", AsyncMock(side_effect=RuntimeError))
    assert await fleet_state._pr_status_cached(repository.BASE_BRANCH) is None
    assert await fleet_state._pr_status_cached("") is None


# --------------------------------------------------------------------------
# trusted binary resolution
# --------------------------------------------------------------------------
def test_trusted_bin_honours_operator_absolute_override(monkeypatch, tmp_path):
    """An absolute executable named in the service env wins outright."""
    tool = tmp_path / "gh-override"
    tool.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    monkeypatch.setenv(runtime._bin_override_var("gh"), str(tool))
    monkeypatch.setattr(runtime, "_TRUSTED_BIN_DIRS", ())

    assert runtime._trusted_bin("gh") == str(tool)
    # Cached, so a later env change cannot repoint an already-vetted tool.
    monkeypatch.delenv(runtime._bin_override_var("gh"))
    assert runtime._trusted_bin("gh") == str(tool)


def test_trusted_bin_ignores_relative_override(monkeypatch, tmp_path):
    """A non-absolute override is discarded rather than PATH-resolved."""
    monkeypatch.setenv(runtime._bin_override_var("gh"), "gh")
    monkeypatch.setattr(runtime, "_TRUSTED_BIN_DIRS", (str(tmp_path),))
    assert runtime._trusted_bin("gh") is None


def test_trusted_bin_rejects_candidate_under_home(monkeypatch, tmp_path):
    """A bin dir whose resolved target sits under $HOME fails closed."""
    home = tmp_path / "home"
    binder = home / "bin"
    binder.mkdir(parents=True)
    tool = binder / "git"
    tool.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    monkeypatch.delenv(runtime._bin_override_var("git"), raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(runtime, "_TRUSTED_BIN_DIRS", (str(binder),))

    assert runtime._trusted_bin("git") is None


def test_trusted_bin_rejects_self_writable_target(monkeypatch, tmp_path):
    """A binary we can write is a plantable shim, never a trusted tool."""
    binder = tmp_path / "sysbin"
    binder.mkdir()
    tool = binder / "git"
    tool.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    monkeypatch.delenv(runtime._bin_override_var("git"), raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(runtime, "_TRUSTED_BIN_DIRS", (str(binder),))

    assert runtime._trusted_bin("git") is None


def test_trusted_bin_tolerates_filesystem_error(monkeypatch, tmp_path):
    """An OSError while vetting a candidate skips it instead of propagating."""
    binder = tmp_path / "sysbin2"
    binder.mkdir()
    tool = binder / "git"
    tool.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    monkeypatch.delenv(runtime._bin_override_var("git"), raising=False)
    monkeypatch.setattr(runtime, "_TRUSTED_BIN_DIRS", (str(binder),))
    real_access = os.access

    def _flaky(path, mode, **kwargs):
        if str(path) == str(tool):
            raise OSError("EIO")
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(runtime.os, "access", _flaky)

    assert runtime._trusted_bin("git") is None


# --------------------------------------------------------------------------
# credential helper loading
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sanitize_helper_rejects_gh_shape_without_trusted_gh(monkeypatch):
    """The gh helper shape is only accepted when gh itself resolves trusted."""
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: None)
    assert runtime._sanitize_helper_value("!/opt/gh auth git-credential") is None

    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/gh")
    assert (
        runtime._sanitize_helper_value("!/opt/gh auth git-credential")
        == "!/usr/bin/gh auth git-credential"
    )


@pytest.mark.asyncio
async def test_load_trusted_helpers_skips_unverifiable_and_counts(monkeypatch, caplog):
    """A rejected helper is logged by KEY only and never enters the env."""
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/gh")
    _run_cmd_queue(
        monkeypatch,
        [
            (0, "credential.helper store\ncredential.helper osxkeychain\n", ""),
            (1, "", ""),
        ],
    )

    with caplog.at_level("WARNING"):
        await repository._load_trusted_credential_helpers()

    helpers = runtime._GIT_TRUSTED_HELPERS
    assert helpers is not None
    values = [v for k, v in helpers.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert values == ["osxkeychain"]
    assert helpers["GIT_CONFIG_COUNT"] == "5"
    assert "store" not in caplog.text
    assert "credential.helper" in caplog.text


@pytest.mark.asyncio
async def test_load_trusted_helpers_caps_at_nine_entries(monkeypatch):
    """The env slot budget stops the scan rather than overflowing GIT_CONFIG_*."""
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: "/usr/bin/gh")
    many = "\n".join(["credential.helper libsecret"] * 12) + "\n"
    _run_cmd_queue(monkeypatch, [(0, many, ""), (0, many, "")])

    await repository._load_trusted_credential_helpers()

    helpers = runtime._GIT_TRUSTED_HELPERS
    assert helpers is not None
    assert len([k for k in helpers if k.startswith("GIT_CONFIG_KEY_")]) == 9
    assert helpers["GIT_CONFIG_COUNT"] == "13"


@pytest.mark.asyncio
async def test_load_trusted_helpers_empty_when_no_config(monkeypatch):
    _run_cmd_queue(monkeypatch, [(1, "", ""), (0, "", "")])
    await repository._load_trusted_credential_helpers()
    assert runtime._GIT_TRUSTED_HELPERS == {}


# --------------------------------------------------------------------------
# live-checkout resolution
# --------------------------------------------------------------------------
def test_same_path_false_on_oserror(monkeypatch):
    """An unresolvable path compares unequal instead of raising."""
    real_resolve = Path.resolve

    def _boom(self, *a, **kw):
        if self.name == "explodes":
            raise OSError("ELOOP")
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(repository.Path, "resolve", _boom)
    assert repository._same_path("/tmp/explodes", "/tmp/explodes") is False


def test_launchd_live_worktree_none_when_exec_is_not_a_venv_binary(monkeypatch, tmp_path):
    """A launcher pointed at a system kirocrew names no worktree."""
    script = tmp_path / "live-gateway"
    script.write_text(
        "#!/bin/sh\nexec '/usr/bin/kirocrew' gateway\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: script)
    )
    assert live._launchd_live_worktree() is None


def test_launchd_live_worktree_none_without_exec_line(monkeypatch, tmp_path):
    script = tmp_path / "live-gateway"
    script.write_text("#!/bin/sh\nsleep 1\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: script)
    )
    assert live._launchd_live_worktree() is None


def test_launchd_live_worktree_resolves_venv_grandparent(monkeypatch, tmp_path):
    checkout = tmp_path / "wt-feat"
    exe = checkout / ".venv" / "bin" / "kirocrew"
    script = tmp_path / "live-gateway"
    script.write_text(f"#!/bin/sh\nexec '{exe}' gateway\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: script)
    )
    assert live._launchd_live_worktree() == str(checkout.resolve())


@pytest.mark.asyncio
async def test_live_worktree_path_uses_launchd_on_darwin(monkeypatch):
    monkeypatch.setattr(live.live_target, "read_target", lambda: None)
    monkeypatch.setattr(live.sys, "platform", "darwin")
    monkeypatch.setattr(live.shutil, "which", lambda name: "/bin/launchctl")
    monkeypatch.setattr(live, "_launchd_live_worktree", lambda: "/checkouts/wt")

    assert await live._live_worktree_path(fresh=True) == "/checkouts/wt"


@pytest.mark.asyncio
async def test_live_worktree_path_none_without_systemd(monkeypatch):
    monkeypatch.setattr(live.live_target, "read_target", lambda: None)
    monkeypatch.setattr(live.sys, "platform", "win32")
    monkeypatch.setattr(live.shutil, "which", lambda name: None)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(side_effect=RuntimeError))

    assert await live._live_worktree_path(fresh=True) is None


@pytest.mark.asyncio
async def test_live_worktree_path_falls_back_to_execstart(monkeypatch):
    """An older unit with no WorkingDirectory is read off ExecStart's path=."""
    # A fabricated, space-free path: the regex the product uses truncates at the
    # first space, so a tmp_path containing one would make this assert the wrong
    # thing on some runners.
    checkout = Path("/opt/kirocrew-checkouts/co")
    exe = checkout / ".venv" / "bin" / "kirocrew"
    monkeypatch.setattr(live.live_target, "read_target", lambda: None)
    monkeypatch.setattr(live.sys, "platform", "linux")
    monkeypatch.setattr(live.shutil, "which", lambda name: "/bin/systemctl")
    _run_cmd_queue(
        monkeypatch,
        [
            (0, "\n", ""),
            (0, f"ExecStart={{ path={exe} ; argv[]=... }}", ""),
        ],
    )

    assert await live._live_worktree_path(fresh=True) == str(checkout.resolve())


@pytest.mark.asyncio
async def test_live_worktree_path_prefers_working_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(live.live_target, "read_target", lambda: None)
    monkeypatch.setattr(live.sys, "platform", "linux")
    monkeypatch.setattr(live.shutil, "which", lambda name: "/bin/systemctl")
    seen = _run_cmd_queue(monkeypatch, [(0, f"{tmp_path}\n", "")])

    assert await live._live_worktree_path(fresh=True) == str(tmp_path.resolve())
    # The ExecStart fallback is not consulted when WorkingDirectory answers.
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_live_worktree_path_serves_cache_until_ttl(monkeypatch):
    """Only ``fresh=True`` bypasses the display cache."""
    monkeypatch.setattr(live, "_LIVE_WORKTREE", "/cached")
    monkeypatch.setattr(live, "_LIVE_CHECK_AT", live.time.monotonic())
    monkeypatch.setattr(live.live_target, "read_target", lambda: None)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(side_effect=RuntimeError))

    assert await live._live_worktree_path() == "/cached"


# --------------------------------------------------------------------------
# _find_worktree_by_path
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_find_worktree_by_path_rejects_empty(monkeypatch):
    monkeypatch.setattr(repository, "_discover_worktrees", AsyncMock(side_effect=RuntimeError))
    target, err = await repository._find_worktree_by_path("")
    assert target is None
    assert err == "'path' must be a non-empty string"


@pytest.mark.asyncio
async def test_find_worktree_by_path_rejects_unresolvable(monkeypatch):
    """A NUL byte cannot be resolved: reported as invalid, never enumerated."""
    monkeypatch.setattr(repository, "_discover_worktrees", AsyncMock(side_effect=RuntimeError))
    target, err = await repository._find_worktree_by_path("/wt/\x00bad")
    assert target is None
    assert err is not None and err.startswith("invalid path:")


@pytest.mark.asyncio
async def test_find_worktree_by_path_matches_known_worktree(monkeypatch, tmp_path):
    wt = {"name": "feat", "path": str(tmp_path)}
    monkeypatch.setattr(repository, "_discover_worktrees", AsyncMock(return_value=[wt]))
    assert await repository._find_worktree_by_path(str(tmp_path)) == (wt, None)


@pytest.mark.asyncio
async def test_find_worktree_by_path_refuses_unknown_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(return_value=[{"name": "feat", "path": str(tmp_path / "other")}]),
    )
    target, err = await repository._find_worktree_by_path(str(tmp_path / "mine"))
    assert target is None
    assert err is not None and err.startswith("path is not a known worktree:")


# --------------------------------------------------------------------------
# _dropin_path
# --------------------------------------------------------------------------
def test_dropin_path_honours_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    got = live._dropin_path()
    assert got.name == "make-live.conf"
    assert got.parent.name == f"{live._LIVE_GATEWAY_UNIT}.d"
    assert got.is_relative_to(tmp_path / "xdg")


def test_dropin_path_falls_back_to_home_config(monkeypatch, tmp_path):
    home = tmp_path / "h"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert live._dropin_path() == (
        home / ".config" / "systemd" / "user" / f"{live._LIVE_GATEWAY_UNIT}.d" / "make-live.conf"
    )


# --------------------------------------------------------------------------
# _restart_gateway
# --------------------------------------------------------------------------
class _FakeBackend:
    """Service-backend double for the restart paths."""

    def __init__(self, *, active: bool = True, ok: bool = True, err: str = "") -> None:
        self._active = active
        self._ok = ok
        self._err = err
        self.restarts = 0

    async def active(self) -> bool:
        return self._active

    async def status(self) -> str:
        return gateway_service.STATUS_OK

    async def restart_detached(self) -> tuple[bool, str]:
        self.restarts += 1
        return self._ok, self._err


@pytest.mark.asyncio
async def test_restart_gateway_refuses_when_cutover_committed(monkeypatch):
    monkeypatch.setattr(live, "_MAKE_LIVE_COMMITTED", True)
    monkeypatch.setattr(live, "_gateway_backend", lambda: pytest.fail("must not probe"))

    out = await live._restart_gateway()

    assert out["ok"] is False
    assert "Make Live cutover is in progress" in out["error"]


@pytest.mark.asyncio
async def test_restart_gateway_refuses_while_make_live_lock_held(monkeypatch):
    lock = asyncio.Lock()
    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", lock)
    monkeypatch.setattr(live, "_gateway_backend", lambda: pytest.fail("must not probe"))

    async with lock:
        out = await live._restart_gateway()

    assert out["ok"] is False
    assert "Make Live cutover is in progress" in out["error"]


@pytest.mark.asyncio
async def test_restart_gateway_uses_service_backend(monkeypatch):
    svc = _FakeBackend(active=True, ok=True)
    monkeypatch.setattr(live, "_gateway_backend", lambda: svc)
    monkeypatch.setattr(live, "_gateway_start_id", AsyncMock(return_value="stamp-1"))

    out = await live._restart_gateway()

    assert out == {"ok": True, "start_id": "stamp-1"}
    assert svc.restarts == 1
    # Latched so a second restart cannot race the pending one.
    assert live._MAKE_LIVE_COMMITTED is True


@pytest.mark.asyncio
async def test_restart_gateway_reports_service_failure_without_latching(monkeypatch):
    svc = _FakeBackend(active=True, ok=False, err="Job failed")
    monkeypatch.setattr(live, "_gateway_backend", lambda: svc)
    monkeypatch.setattr(live, "_gateway_start_id", AsyncMock(return_value=None))

    out = await live._restart_gateway()

    assert out == {"ok": False, "error": "Job failed"}
    assert live._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_restart_gateway_falls_back_to_foreground(monkeypatch):
    """No drivable manager: the detached foreground respawn is the last resort."""
    monkeypatch.setattr(live, "_gateway_backend", lambda: None)
    monkeypatch.setattr(live, "_live_user_unit_status", AsyncMock(return_value="no_user_unit"))
    fg = _FakeBackend(ok=True)
    monkeypatch.setattr(live, "_foreground_backend", lambda: fg)
    monkeypatch.setattr(live, "_gateway_start_id", AsyncMock(return_value="pid-77"))

    out = await live._restart_gateway()

    assert out == {"ok": True, "start_id": "pid-77"}
    assert fg.restarts == 1


@pytest.mark.asyncio
async def test_restart_gateway_reports_foreground_failure(monkeypatch):
    monkeypatch.setattr(live, "_gateway_backend", lambda: _FakeBackend(active=False))
    monkeypatch.setattr(live, "_live_user_unit_status", AsyncMock(return_value="no_agent"))
    monkeypatch.setattr(
        live, "_foreground_backend", lambda: _FakeBackend(ok=False, err="no marker")
    )
    monkeypatch.setattr(live, "_gateway_start_id", AsyncMock(return_value=None))

    out = await live._restart_gateway()

    assert out == {"ok": False, "error": "no marker"}
    assert live._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_restart_gateway_refuses_confined_status(monkeypatch):
    """A mis-set-up manager keeps its named remedy instead of a blind respawn."""
    monkeypatch.setattr(live, "_gateway_backend", lambda: _FakeBackend(active=False))
    monkeypatch.setattr(
        live, "_live_user_unit_status", AsyncMock(return_value="user_unit_inactive")
    )
    monkeypatch.setattr(live, "_foreground_backend", lambda: pytest.fail("not eligible"))

    out = await live._restart_gateway()

    assert out["ok"] is False
    # The error message now comes from _make_live_status_error, surfacing the
    # specific reason the gateway cannot be restarted, plus the manual remedy.
    assert "user service" in out["error"]
    assert "not running" in out["error"]
    assert "kirocrew restart" in out["error"]


@pytest.mark.asyncio
async def test_restart_gateway_handler_returns_result(monkeypatch):
    monkeypatch.setattr(runtime, "_sel", lambda: _NullSel())
    monkeypatch.setattr(live, "_restart_gateway", AsyncMock(return_value={"ok": True}))

    request = make_mocked_request("POST", "/api/restart-gateway")
    resp = await http_api.api_dev_fleet_restart_gateway(request)

    assert resp.status == 200
    assert json.loads(resp.text) == {"ok": True}


class _NullSel:
    def log_tool_invocation(self, **kw) -> None:  # pragma: no cover - sink
        pass


def _body_request(raw: bytes) -> MagicMock:
    """Request double shaped like the one _audited + _json_body consume."""
    request = MagicMock()
    request.read = AsyncMock(return_value=raw)
    try:
        request.json = AsyncMock(return_value=json.loads(raw or b"{}"))
    except ValueError as exc:
        request.json = AsyncMock(side_effect=exc)
    request.content_length = len(raw)
    request.can_read_body = True
    return request


@pytest.mark.asyncio
async def test_make_live_handler_rejects_unparseable_body(monkeypatch):
    monkeypatch.setattr(runtime, "_sel", lambda: _NullSel())
    monkeypatch.setattr(live, "_make_live", AsyncMock(side_effect=RuntimeError))

    resp = await http_api.api_dev_fleet_make_live(_body_request(b"{not json"))

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid JSON body"}


@pytest.mark.asyncio
async def test_make_live_handler_requires_path_string(monkeypatch):
    monkeypatch.setattr(runtime, "_sel", lambda: _NullSel())
    monkeypatch.setattr(live, "_make_live", AsyncMock(side_effect=RuntimeError))
    raw = json.dumps({"path": 12}).encode()

    resp = await http_api.api_dev_fleet_make_live(_body_request(raw))

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "'path' must be a non-empty string"}


@pytest.mark.asyncio
async def test_make_live_handler_validates_dry_run_type(monkeypatch):
    monkeypatch.setattr(runtime, "_sel", lambda: _NullSel())
    monkeypatch.setattr(live, "_make_live", AsyncMock(side_effect=RuntimeError))
    raw = json.dumps({"path": "/wt/feat", "dry_run": "yes"}).encode()

    resp = await http_api.api_dev_fleet_make_live(_body_request(raw))

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "dry_run must be a boolean"}


@pytest.mark.asyncio
async def test_make_live_handler_passes_dry_run_through(monkeypatch):
    monkeypatch.setattr(runtime, "_sel", lambda: _NullSel())
    make_live = AsyncMock(return_value={"ok": True, "dry_run": True})
    monkeypatch.setattr(live, "_make_live", make_live)
    raw = json.dumps({"path": "/wt/feat", "dry_run": True}).encode()

    resp = await http_api.api_dev_fleet_make_live(_body_request(raw))

    assert resp.status == 200
    make_live.assert_awaited_once_with("/wt/feat", True, expected_staged=None)


@pytest.mark.asyncio
async def test_make_live_refuses_missing_worktree_path(monkeypatch, tmp_path):
    """A known worktree whose directory is gone is refused before any mutation."""
    gone = tmp_path / "removed"
    monkeypatch.setattr(
        repository,
        "_find_worktree_by_path",
        AsyncMock(return_value=({"name": "removed", "path": str(gone)}, None)),
    )
    monkeypatch.setattr(live, "_in_pod", lambda: pytest.fail("checked too late"))

    out = await live._make_live(str(gone))

    assert out["ok"] is False
    assert out["code"] == "missing_path"


def test_kill_tree_sync_kills_descendants_first(monkeypatch):
    """Descendants are enumerated before the group kill erases their PPIDs."""
    order: list[str] = []
    monkeypatch.setattr(
        platform_compat,
        "process_descendants",
        lambda pid: (order.append(f"enum:{pid}"), [11, 12])[1],
    )

    def _kill(pid: int) -> None:
        order.append(f"kill:{pid}")
        if pid == 11:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(platform_compat, "kill_process_tree", _kill)

    runtime._kill_tree_sync(7)

    assert order == ["enum:7", "kill:7", "kill:11", "kill:12"]


def test_kill_tree_sync_tolerates_primary_kill_failure(monkeypatch):
    """A group kill that fails still lets the descendant sweep run."""
    killed: list[int] = []
    monkeypatch.setattr(platform_compat, "process_descendants", lambda pid: [21])

    def _kill(pid: int) -> None:
        killed.append(pid)
        if pid == 9:
            raise OSError("EPERM")

    monkeypatch.setattr(platform_compat, "kill_process_tree", _kill)

    runtime._kill_tree_sync(9)

    assert killed == [9, 21]


# --------------------------------------------------------------------------
# pod config + request-body validation
# --------------------------------------------------------------------------
def test_load_cfg_none_when_pod_config_unloadable(monkeypatch):
    """A pod config that will not load degrades to None, never an exception."""
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)

    class _Boom:
        @staticmethod
        def load():
            raise RuntimeError("no pods dir")

    monkeypatch.setattr(runtime, "PodConfig", _Boom, raising=False)
    assert runtime._load_cfg() is None


def test_load_cfg_none_when_pods_unavailable(monkeypatch):
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", False)
    assert runtime._load_cfg() is None


@pytest.mark.asyncio
async def test_worktree_remove_handler_rejects_unparseable_body(monkeypatch):
    monkeypatch.setattr(runtime, "_sel", lambda: _NullSel())
    monkeypatch.setattr(worktree_ops, "_worktree_remove", AsyncMock(side_effect=RuntimeError))

    resp = await http_api.api_dev_fleet_worktree_remove(_body_request(b"[1, 2]"))

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "body must be an object"}


@pytest.mark.asyncio
async def test_pod_name_action_rejects_unparseable_body(monkeypatch):
    action = AsyncMock(side_effect=RuntimeError)
    resp = await http_api._pod_name_action(_body_request(b"nope"), action)

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid JSON body"}
    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_pod_name_action_requires_known_worktree(monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", AsyncMock(return_value=(None, "no such worktree"))
    )
    action = AsyncMock(side_effect=RuntimeError)

    resp = await http_api._pod_name_action(_body_request(b'{"name": "ghost"}'), action)

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "no such worktree"}
    action.assert_not_awaited()


# --------------------------------------------------------------------------
# service drivability probes
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gateway_service_reason_none_when_drivable(monkeypatch):
    monkeypatch.setattr(live, "_gateway_service_active", AsyncMock(return_value=True))
    assert await live._gateway_service_reason() is None


@pytest.mark.asyncio
async def test_gateway_service_reason_appends_unknown_checkout_hint(monkeypatch):
    """An unattributable gateway gets the extra Pull+Build caveat."""
    monkeypatch.setattr(live, "_gateway_service_active", AsyncMock(return_value=False))
    monkeypatch.setattr(live, "_live_user_unit_status", AsyncMock(return_value="no_agent"))
    monkeypatch.setattr(live, "_live_worktree_path", AsyncMock(return_value=None))

    reason = await live._gateway_service_reason()

    assert reason is not None
    assert "does not belong to any known worktree" in reason


@pytest.mark.asyncio
async def test_gateway_service_reason_omits_hint_for_known_checkout(monkeypatch):
    monkeypatch.setattr(live, "_gateway_service_active", AsyncMock(return_value=False))
    monkeypatch.setattr(
        live, "_live_user_unit_status", AsyncMock(return_value="user_unit_inactive")
    )
    monkeypatch.setattr(live, "_live_worktree_path", AsyncMock(return_value="/co"))

    reason = await live._gateway_service_reason()

    assert reason is not None
    assert "does not belong to any known worktree" not in reason


@pytest.mark.asyncio
async def test_gateway_service_active_accepts_foreground_backend(monkeypatch):
    """With no drivable manager, an unconfined foreground backend still counts."""
    monkeypatch.setattr(live, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live, "_gateway_backend", lambda: None)
    monkeypatch.setattr(live, "_live_user_unit_status", AsyncMock(return_value="no_systemd"))
    monkeypatch.setattr(live, "_foreground_backend", lambda: _FakeBackend())

    assert await live._gateway_service_active() is True
    assert live._GATEWAY_SERVICE_ACTIVE is True


@pytest.mark.asyncio
async def test_gateway_service_active_false_when_foreground_confined(monkeypatch):
    monkeypatch.setattr(live, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(live, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(live, "_gateway_backend", lambda: _FakeBackend(active=False))
    monkeypatch.setattr(
        live, "_live_user_unit_status", AsyncMock(return_value="user_unit_inactive")
    )
    monkeypatch.setattr(live, "_foreground_backend", lambda: pytest.fail("not eligible"))

    assert await live._gateway_service_active() is False


# --- stale sync lock race (issue #4906) ---


@pytest.mark.asyncio
async def test_sync_allows_new_run_when_prior_task_done_but_status_stale(monkeypatch):
    """When the subprocess has exited (proc.returncode is not None) but _RUNS
    status is still 'running' (stale due to a lock-acquisition race), _sync()
    should await the task briefly, then allow a new sync."""
    import asyncio

    # Simulate a completed task with a process that has already exited
    done_task = asyncio.ensure_future(asyncio.sleep(0))
    await done_task  # let it complete

    # Mock a subprocess whose returncode signals exit
    mock_proc = MagicMock()
    mock_proc.returncode = 0  # process exited

    old_rid = "stale-run-001"
    monkeypatch.setattr(worktree_ops, "_SYNC_RID", old_rid)
    monkeypatch.setattr(runtime, "_SYNC_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_RUNS_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        runtime,
        "_RUNS",
        {
            old_rid: {"status": "running", "exit_code": None, "output": []},
        },
    )
    # Task done + proc.returncode set — simulates the race window
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {old_rid: (done_task, mock_proc)})

    # _sync_start_locked would normally start a new sync; mock it to confirm
    # we reach it (rather than getting the 'already running' refusal)
    new_rid = "new-run-002"
    monkeypatch.setattr(
        worktree_ops, "_sync_start_locked", AsyncMock(return_value={"ok": True, "run_id": new_rid})
    )

    result = await worktree_ops._sync()
    assert result["ok"] is True
    assert result["run_id"] == new_rid


@pytest.mark.asyncio
async def test_sync_refuses_when_task_genuinely_running(monkeypatch):
    """When the subprocess is still alive (proc.returncode is None), _sync()
    should correctly refuse."""
    import asyncio

    # A task that hasn't completed yet
    never_done = asyncio.get_event_loop().create_future()
    running_task = asyncio.ensure_future(never_done)

    # Mock a subprocess still running
    mock_proc = MagicMock()
    mock_proc.returncode = None  # process still alive

    old_rid = "active-run-001"
    monkeypatch.setattr(worktree_ops, "_SYNC_RID", old_rid)
    monkeypatch.setattr(runtime, "_SYNC_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_RUNS_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        runtime,
        "_RUNS",
        {
            old_rid: {"status": "running", "exit_code": None, "output": []},
        },
    )
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {old_rid: (running_task, mock_proc)})

    result = await worktree_ops._sync()
    assert result["ok"] is False
    assert "already running" in result["error"]
    assert result["run_id"] == old_rid

    # Cleanup
    never_done.set_result(None)
    await running_task


@pytest.mark.asyncio
async def test_sync_allows_new_run_when_task_absent_from_active_runs(monkeypatch):
    """When the run is not in _ACTIVE_RUNS at all (task completed and was
    cleaned up by done_callback), _sync() should allow a new sync."""
    import asyncio

    old_rid = "gone-run-001"
    monkeypatch.setattr(worktree_ops, "_SYNC_RID", old_rid)
    monkeypatch.setattr(runtime, "_SYNC_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_RUNS_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        runtime,
        "_RUNS",
        {
            old_rid: {"status": "running", "exit_code": None, "output": []},
        },
    )
    # Empty — task was already cleaned up by done_callback
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})

    monkeypatch.setattr(
        worktree_ops,
        "_sync_start_locked",
        AsyncMock(return_value={"ok": True, "run_id": "fresh-run"}),
    )

    result = await worktree_ops._sync()
    assert result["ok"] is True
    assert result["run_id"] == "fresh-run"


@pytest.mark.asyncio
async def test_sync_proc_not_yet_spawned_refuses(monkeypatch):
    """When the active entry has proc=None (subprocess not yet spawned),
    _sync() should refuse — the sync is genuinely starting up."""
    import asyncio

    never_done = asyncio.get_event_loop().create_future()
    running_task = asyncio.ensure_future(never_done)

    old_rid = "spawning-run-001"
    monkeypatch.setattr(worktree_ops, "_SYNC_RID", old_rid)
    monkeypatch.setattr(runtime, "_SYNC_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_RUNS_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        runtime,
        "_RUNS",
        {
            old_rid: {"status": "running", "exit_code": None, "output": []},
        },
    )
    # proc=None means subprocess hasn't been spawned yet
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {old_rid: (running_task, None)})

    result = await worktree_ops._sync()
    assert result["ok"] is False
    assert "already running" in result["error"]

    # Cleanup
    never_done.set_result(None)
    await running_task


@pytest.mark.asyncio
async def test_sync_refuses_when_worker_cleanup_times_out(monkeypatch):
    """When the process exited but the worker task does not complete within
    the 2s bounded wait (cleanup is slow), _sync() should refuse rather
    than starting a concurrent sync against the same worktree."""
    import asyncio

    # A task that will NOT complete within 2s (simulates slow cleanup)
    never_done = asyncio.get_event_loop().create_future()
    slow_task = asyncio.ensure_future(never_done)

    # Mock a subprocess that has exited (returncode set)
    mock_proc = MagicMock()
    mock_proc.returncode = 0

    old_rid = "slow-cleanup-001"
    monkeypatch.setattr(worktree_ops, "_SYNC_RID", old_rid)
    monkeypatch.setattr(runtime, "_SYNC_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_RUNS_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        runtime,
        "_RUNS",
        {
            old_rid: {"status": "running", "exit_code": None, "output": []},
        },
    )
    # Process exited but task won't finish (simulating slow cleanup)
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {old_rid: (slow_task, mock_proc)})

    result = await worktree_ops._sync()
    assert result["ok"] is False
    assert "already running" in result["error"]

    # Cleanup
    never_done.set_result(None)
    await slow_task
