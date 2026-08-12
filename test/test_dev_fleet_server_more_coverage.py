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

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import gateway_service


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
        if self._communicate_delay is not None:
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

    monkeypatch.setattr(mod, "create_subprocess_limited", _fake)
    return calls


def _spawn_raises(monkeypatch, exc: BaseException) -> None:
    async def _fake(*argv, **kwargs):
        raise exc

    monkeypatch.setattr(mod, "create_subprocess_limited", _fake)


def _passthrough_sandbox(monkeypatch, cleanup: str | None = None) -> None:
    """``sandboxed_spawn_argv`` double: identity argv, no OS isolation."""
    monkeypatch.setattr(
        mod,
        "sandboxed_spawn_argv",
        lambda argv, tier, env=None: (list(argv), dict(env or {}), cleanup),
    )


def _run_cmd_queue(monkeypatch, results: list[tuple[int, str, str]]) -> list[list[str]]:
    """Replace ``_run_cmd`` with a scripted queue; return the argv log."""
    seen: list[list[str]] = []

    async def _fake(cmd, **kwargs):
        seen.append(list(cmd))
        return results[len(seen) - 1] if len(seen) <= len(results) else (1, "", "")

    monkeypatch.setattr(mod, "_run_cmd", _fake)
    return seen


async def _drain_run(rid: str) -> dict:
    """Let the _start_run worker task finish, then return its record."""
    for _ in range(2000):
        if mod._RUNS[rid]["status"] != "running":
            return mod._RUNS[rid]
        await asyncio.sleep(0)
    raise AssertionError(f"run {rid} never left 'running'")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset the module caches these tests read or write."""
    monkeypatch.setattr(mod, "_RUNS", {})
    monkeypatch.setattr(mod, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(mod, "_PR_CACHE", {})
    monkeypatch.setattr(mod, "_FALLBACK_REPOS", [])
    monkeypatch.setattr(mod, "_OWNER_REPO", None)
    monkeypatch.setattr(mod, "_OWNER_REPO_RETRY_AT", 0.0)
    monkeypatch.setattr(mod, "_TRUSTED_BIN_CACHE", {})
    monkeypatch.setattr(mod, "_GIT_TRUSTED_HELPERS", None)
    monkeypatch.setattr(mod, "_LIVE_WORKTREE", None)
    monkeypatch.setattr(mod, "_LIVE_CHECK_AT", 0.0)
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(mod, "_MAKE_LIVE_LOCK", asyncio.Lock())


# --------------------------------------------------------------------------
# _run_cmd -- the sandboxed spawn chokepoint
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_cmd_refuses_unresolvable_bare_tool(monkeypatch):
    """A bare command name that resolves to no trusted binary never spawns."""
    monkeypatch.setattr(mod, "_trusted_bin", lambda name: None)
    _spawn_raises(monkeypatch, AssertionError("must not spawn"))

    rc, out, err = await mod._run_cmd(["git", "status"])

    assert (rc, out) == (-1, "")
    assert err.startswith(mod._UNRESOLVED_TOOL_PREFIX)
    assert "'git'" in err


@pytest.mark.asyncio
async def test_run_cmd_spawn_oserror_deletes_sandbox_cleanup_file(monkeypatch, tmp_path):
    """An OSError from the spawn reports it AND removes the launcher temp file."""
    leftover = tmp_path / "launcher.sh"
    leftover.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(mod, "_trusted_bin", lambda name: "/usr/bin/git")
    _passthrough_sandbox(monkeypatch, cleanup=str(leftover))
    _spawn_raises(monkeypatch, OSError("ENOMEM"))

    rc, out, err = await mod._run_cmd(["git", "status"])

    assert (rc, out) == (-1, "")
    assert err == "spawn failed: ENOMEM"
    assert not leftover.exists()


@pytest.mark.asyncio
async def test_run_cmd_timeout_kills_tree_and_reports(monkeypatch, tmp_path):
    """A communicate() that outlives *timeout* is reaped, not left running."""
    monkeypatch.setattr(mod, "_trusted_bin", lambda name: "/usr/bin/git")
    _passthrough_sandbox(monkeypatch, cleanup=str(tmp_path / "never-written"))
    proc = _FakeProc(communicate_delay=5.0)
    _spawn_returns(monkeypatch, proc)
    killed: list[int] = []
    monkeypatch.setattr(mod, "_kill_tree", AsyncMock(side_effect=killed.append))

    rc, out, err = await mod._run_cmd(["git", "status"], timeout=0)

    assert (rc, out, err) == (-1, "", "timeout (0s)")
    assert killed == [proc.pid]
    # kill() + wait() both ran, and the missing cleanup file was tolerated.
    assert (proc.kills, proc.waits) == (1, 1)


@pytest.mark.asyncio
async def test_run_cmd_success_removes_cleanup_file(monkeypatch, tmp_path):
    """The happy path deletes the sandbox launcher in its finally block."""
    launcher = tmp_path / "launcher2.sh"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(mod, "_trusted_bin", lambda name: "/usr/bin/git")
    _passthrough_sandbox(monkeypatch, cleanup=str(launcher))
    _spawn_returns(monkeypatch, _FakeProc(rc=0))

    rc, out, err = await mod._run_cmd(["git", "status"])

    assert (rc, out, err) == (0, "out", "err")
    assert not launcher.exists()


@pytest.mark.asyncio
async def test_kill_tree_swallows_process_lookup_error(monkeypatch):
    """A pid that vanished between enumeration and signalling is not an error."""
    def _boom(pid: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(mod, "_kill_tree_sync", _boom)
    assert await mod._kill_tree(999999) is None


# --------------------------------------------------------------------------
# _start_run -- background streaming worker
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_run_records_spawn_failure(monkeypatch):
    _spawn_raises(monkeypatch, OSError("no fork"))

    rid = await mod._start_run("provision", ["kirocrew", "pod", "up"])
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

    rid = await mod._start_run(
        "build", ["kirocrew", "pod", "provision"],
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
    monkeypatch.setattr(mod, "_RUN_DEADLINE_S", -1)
    proc = _FakeProc(lines=[b"never read\n"], rc=0)
    _spawn_returns(monkeypatch, proc)
    killed: list[int] = []
    monkeypatch.setattr(mod, "_kill_tree", AsyncMock(side_effect=killed.append))

    rid = await mod._start_run("sync", ["kirocrew", "sync"])
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
    monkeypatch.setattr(mod, "_kill_tree", AsyncMock(side_effect=killed.append))

    rid = await mod._start_run("sync", ["kirocrew", "sync"])
    rec = await _drain_run(rid)

    assert rec["status"] == "done"
    assert rec["exit_code"] == -1
    assert rec["output"] == ["[error] line too long"]
    assert killed == [proc.pid]
    assert proc.kills == 1


@pytest.mark.asyncio
async def test_start_run_stream_error_tolerates_already_reaped_child(monkeypatch):
    """kill() raising ProcessLookupError must not mask the original error."""
    proc = _FakeProc(
        readline_error=ValueError("boom"), kill_error=ProcessLookupError(4321)
    )
    _spawn_returns(monkeypatch, proc)
    monkeypatch.setattr(mod, "_kill_tree", AsyncMock())

    rid = await mod._start_run("sync", ["kirocrew", "sync"])
    rec = await _drain_run(rid)

    assert rec["output"] == ["[error] boom"]


# --------------------------------------------------------------------------
# PR query layer (gh / git through _run_cmd)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_repo_owner_name_none_when_remote_lookup_fails(monkeypatch):
    monkeypatch.setattr(mod, "_upstream_remote", AsyncMock(return_value="origin"))
    _run_cmd_queue(monkeypatch, [(1, "", "fatal: no such remote")])
    assert await mod._repo_owner_name() is None


@pytest.mark.asyncio
async def test_repo_owner_name_none_when_url_unparseable(monkeypatch):
    monkeypatch.setattr(mod, "_upstream_remote", AsyncMock(return_value="origin"))
    _run_cmd_queue(monkeypatch, [(0, "not-a-remote-url\n", "")])
    assert await mod._repo_owner_name() is None


@pytest.mark.asyncio
async def test_repo_owner_name_parses_ssh_url(monkeypatch):
    monkeypatch.setattr(mod, "_upstream_remote", AsyncMock(return_value="origin"))
    _run_cmd_queue(monkeypatch, [(0, "git@github.com:kirodotdev/KiroCrew.git\n", "")])
    assert await mod._repo_owner_name() == "kirodotdev/KiroCrew"


@pytest.mark.asyncio
async def test_get_owner_repo_caches_success_and_backs_off_failure(monkeypatch):
    """Success is cached permanently; a failure arms a retry deadline."""
    calls = []

    async def _lookup():
        calls.append(1)
        return None

    monkeypatch.setattr(mod, "_repo_owner_name", _lookup)
    assert await mod._get_owner_repo() is None
    assert mod._OWNER_REPO_RETRY_AT > 0
    # Second call is inside the back-off window: no new lookup.
    assert await mod._get_owner_repo() is None
    assert len(calls) == 1

    monkeypatch.setattr(mod, "_OWNER_REPO_RETRY_AT", 0.0)
    monkeypatch.setattr(mod, "_repo_owner_name", AsyncMock(return_value="o/r"))
    assert await mod._get_owner_repo() == "o/r"
    # Now cached: a lookup that would raise is never reached.
    monkeypatch.setattr(mod, "_repo_owner_name", AsyncMock(side_effect=RuntimeError))
    assert await mod._get_owner_repo() == "o/r"


@pytest.mark.asyncio
async def test_pr_query_one_none_on_gh_failure(monkeypatch):
    _run_cmd_queue(monkeypatch, [(1, "", "gh: not logged in")])
    assert await mod._pr_query_one("o/r", "feat/x") is None


@pytest.mark.asyncio
async def test_pr_query_one_none_on_unparseable_json(monkeypatch):
    _run_cmd_queue(monkeypatch, [(0, "<html>rate limited</html>", "")])
    assert await mod._pr_query_one("o/r", "feat/x") is None


@pytest.mark.asyncio
async def test_pr_query_one_none_on_empty_result(monkeypatch):
    _run_cmd_queue(monkeypatch, [(0, "[]", "")])
    assert await mod._pr_query_one("o/r", "feat/x") is None


@pytest.mark.asyncio
async def test_pr_query_one_moves_body_to_internal_key(monkeypatch):
    """``body`` becomes ``_body`` so _redact_pr drops it from the payload."""
    payload = json.dumps([{"number": 7, "state": "OPEN", "body": None}])
    seen = _run_cmd_queue(monkeypatch, [(0, payload, "")])

    pr = await mod._pr_query_one("o/r", "feat/x")

    assert pr is not None
    assert pr["_repo"] == "o/r"
    assert pr["_body"] == ""
    assert "body" not in pr
    assert "--head" in seen[0] and "feat/x" in seen[0]


@pytest.mark.asyncio
async def test_fetch_pr_status_needs_owner_and_branch(monkeypatch):
    monkeypatch.setattr(mod, "_get_owner_repo", AsyncMock(return_value=None))
    assert await mod._fetch_pr_status("feat/x") is None

    monkeypatch.setattr(mod, "_get_owner_repo", AsyncMock(return_value="o/r"))
    assert await mod._fetch_pr_status("") is None


@pytest.mark.asyncio
async def test_fetch_pr_status_falls_back_to_legacy_remote(monkeypatch):
    """A miss upstream is retried against the ancestor-verified legacy repos."""
    monkeypatch.setattr(mod, "_get_owner_repo", AsyncMock(return_value="new/repo"))
    monkeypatch.setattr(mod, "_FALLBACK_REPOS", ["dead/repo", "old/repo"])
    asked: list[str] = []

    async def _one(owner_repo: str, branch: str):
        asked.append(owner_repo)
        return {"number": 1, "_repo": owner_repo} if owner_repo == "old/repo" else None

    monkeypatch.setattr(mod, "_pr_query_one", _one)

    pr = await mod._fetch_pr_status("feat/x")

    assert pr == {"number": 1, "_repo": "old/repo"}
    assert asked == ["new/repo", "dead/repo", "old/repo"]


@pytest.mark.asyncio
async def test_fetch_pr_status_none_when_no_repo_has_the_branch(monkeypatch):
    monkeypatch.setattr(mod, "_get_owner_repo", AsyncMock(return_value="new/repo"))
    monkeypatch.setattr(mod, "_FALLBACK_REPOS", ["old/repo"])
    monkeypatch.setattr(mod, "_pr_query_one", AsyncMock(return_value=None))
    assert await mod._fetch_pr_status("feat/x") is None


@pytest.mark.asyncio
async def test_head_contained_in_pr_identical_oid_skips_git(monkeypatch):
    _run_cmd_queue(monkeypatch, [])  # any spawn would return (1, "", "")
    assert await mod._head_contained_in_pr("/wt", " abc123 ", "abc123\n") is True


@pytest.mark.asyncio
async def test_head_contained_in_pr_uses_ancestor_check(monkeypatch):
    seen = _run_cmd_queue(monkeypatch, [(0, "", "")])
    assert await mod._head_contained_in_pr("/wt", "aaa", "bbb") is True
    assert seen[0][-3:] == ["--is-ancestor", "aaa", "bbb"]


@pytest.mark.asyncio
async def test_head_contained_in_pr_false_when_diverged(monkeypatch):
    _run_cmd_queue(monkeypatch, [(1, "", "")])
    assert await mod._head_contained_in_pr("/wt", "aaa", "bbb") is False


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_requires_owner_and_branch(monkeypatch):
    monkeypatch.setattr(mod, "_get_owner_repo", AsyncMock(return_value=None))
    assert await mod._fetch_pr_head_oid("feat/x") is None
    assert await mod._fetch_pr_head_oid("", repo="o/r") is None


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_none_when_gh_fails(monkeypatch):
    _run_cmd_queue(monkeypatch, [(1, "", "gh error")])
    assert await mod._fetch_pr_head_oid("feat/x", repo="o/r") is None


@pytest.mark.asyncio
async def test_fetch_pr_head_oid_none_on_bad_json(monkeypatch):
    _run_cmd_queue(monkeypatch, [(0, "not json", "")])
    assert await mod._fetch_pr_head_oid("feat/x", repo="o/r") is None


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
    assert await mod._fetch_pr_head_oid("feat/x", repo="o/r") is None
    assert await mod._fetch_pr_head_oid("feat/x", repo="o/r") == "cafe1234"


@pytest.mark.asyncio
async def test_pr_status_cached_serves_terminal_entry_without_refetch(monkeypatch):
    """A MERGED entry is permanently terminal; a stale OPEN entry refetches."""
    monkeypatch.setattr(
        mod,
        "_PR_CACHE",
        {
            "merged": {"data": {"state": "MERGED"}, "ts": 0.0},
            "stale": {"data": {"state": "OPEN"}, "ts": 0.0},
        },
    )
    fetch = AsyncMock(return_value={"state": "OPEN", "number": 9})
    monkeypatch.setattr(mod, "_fetch_pr_status", fetch)

    assert await mod._pr_status_cached("merged") == {"state": "MERGED"}
    fetch.assert_not_awaited()

    assert await mod._pr_status_cached("stale") == {"state": "OPEN", "number": 9}
    assert mod._PR_CACHE["stale"]["data"]["number"] == 9


@pytest.mark.asyncio
async def test_pr_status_cached_skips_base_branch(monkeypatch):
    monkeypatch.setattr(mod, "_fetch_pr_status", AsyncMock(side_effect=RuntimeError))
    assert await mod._pr_status_cached(mod.BASE_BRANCH) is None
    assert await mod._pr_status_cached("") is None


# --------------------------------------------------------------------------
# trusted binary resolution
# --------------------------------------------------------------------------
def test_trusted_bin_honours_operator_absolute_override(monkeypatch, tmp_path):
    """An absolute executable named in the service env wins outright."""
    tool = tmp_path / "gh-override"
    tool.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    monkeypatch.setenv(mod._bin_override_var("gh"), str(tool))
    monkeypatch.setattr(mod, "_TRUSTED_BIN_DIRS", ())

    assert mod._trusted_bin("gh") == str(tool)
    # Cached, so a later env change cannot repoint an already-vetted tool.
    monkeypatch.delenv(mod._bin_override_var("gh"))
    assert mod._trusted_bin("gh") == str(tool)


def test_trusted_bin_ignores_relative_override(monkeypatch, tmp_path):
    """A non-absolute override is discarded rather than PATH-resolved."""
    monkeypatch.setenv(mod._bin_override_var("gh"), "gh")
    monkeypatch.setattr(mod, "_TRUSTED_BIN_DIRS", (str(tmp_path),))
    assert mod._trusted_bin("gh") is None


def test_trusted_bin_rejects_candidate_under_home(monkeypatch, tmp_path):
    """A bin dir whose resolved target sits under $HOME fails closed."""
    home = tmp_path / "home"
    binder = home / "bin"
    binder.mkdir(parents=True)
    tool = binder / "git"
    tool.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    monkeypatch.delenv(mod._bin_override_var("git"), raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(mod, "_TRUSTED_BIN_DIRS", (str(binder),))

    assert mod._trusted_bin("git") is None


def test_trusted_bin_rejects_self_writable_target(monkeypatch, tmp_path):
    """A binary we can write is a plantable shim, never a trusted tool."""
    binder = tmp_path / "sysbin"
    binder.mkdir()
    tool = binder / "git"
    tool.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    monkeypatch.delenv(mod._bin_override_var("git"), raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(mod, "_TRUSTED_BIN_DIRS", (str(binder),))

    assert mod._trusted_bin("git") is None


def test_trusted_bin_tolerates_filesystem_error(monkeypatch, tmp_path):
    """An OSError while vetting a candidate skips it instead of propagating."""
    binder = tmp_path / "sysbin2"
    binder.mkdir()
    tool = binder / "git"
    tool.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)
    monkeypatch.delenv(mod._bin_override_var("git"), raising=False)
    monkeypatch.setattr(mod, "_TRUSTED_BIN_DIRS", (str(binder),))
    real_access = os.access

    def _flaky(path, mode, **kwargs):
        if str(path) == str(tool):
            raise OSError("EIO")
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(mod.os, "access", _flaky)

    assert mod._trusted_bin("git") is None


# --------------------------------------------------------------------------
# credential helper loading
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sanitize_helper_rejects_gh_shape_without_trusted_gh(monkeypatch):
    """The gh helper shape is only accepted when gh itself resolves trusted."""
    monkeypatch.setattr(mod, "_trusted_bin", lambda name: None)
    assert mod._sanitize_helper_value("!/opt/gh auth git-credential") is None

    monkeypatch.setattr(mod, "_trusted_bin", lambda name: "/usr/bin/gh")
    assert (
        mod._sanitize_helper_value("!/opt/gh auth git-credential")
        == "!/usr/bin/gh auth git-credential"
    )


@pytest.mark.asyncio
async def test_load_trusted_helpers_skips_unverifiable_and_counts(monkeypatch, caplog):
    """A rejected helper is logged by KEY only and never enters the env."""
    monkeypatch.setattr(mod, "_trusted_bin", lambda name: "/usr/bin/gh")
    _run_cmd_queue(
        monkeypatch,
        [
            (0, "credential.helper store\ncredential.helper osxkeychain\n", ""),
            (1, "", ""),
        ],
    )

    with caplog.at_level("WARNING"):
        await mod._load_trusted_credential_helpers()

    helpers = mod._GIT_TRUSTED_HELPERS
    assert helpers is not None
    values = [v for k, v in helpers.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert values == ["osxkeychain"]
    assert helpers["GIT_CONFIG_COUNT"] == "5"
    assert "store" not in caplog.text
    assert "credential.helper" in caplog.text


@pytest.mark.asyncio
async def test_load_trusted_helpers_caps_at_nine_entries(monkeypatch):
    """The env slot budget stops the scan rather than overflowing GIT_CONFIG_*."""
    monkeypatch.setattr(mod, "_trusted_bin", lambda name: "/usr/bin/gh")
    many = "\n".join(["credential.helper libsecret"] * 12) + "\n"
    _run_cmd_queue(monkeypatch, [(0, many, ""), (0, many, "")])

    await mod._load_trusted_credential_helpers()

    helpers = mod._GIT_TRUSTED_HELPERS
    assert helpers is not None
    assert len([k for k in helpers if k.startswith("GIT_CONFIG_KEY_")]) == 9
    assert helpers["GIT_CONFIG_COUNT"] == "13"


@pytest.mark.asyncio
async def test_load_trusted_helpers_empty_when_no_config(monkeypatch):
    _run_cmd_queue(monkeypatch, [(1, "", ""), (0, "", "")])
    await mod._load_trusted_credential_helpers()
    assert mod._GIT_TRUSTED_HELPERS == {}


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

    monkeypatch.setattr(mod.Path, "resolve", _boom)
    assert mod._same_path("/tmp/explodes", "/tmp/explodes") is False


def test_launchd_live_worktree_none_when_exec_is_not_a_venv_binary(monkeypatch, tmp_path):
    """A launcher pointed at a system kirocrew names no worktree."""
    script = tmp_path / "live-gateway"
    script.write_text(
        "#!/bin/sh\nexec '/usr/bin/kirocrew' gateway\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: script)
    )
    assert mod._launchd_live_worktree() is None


def test_launchd_live_worktree_none_without_exec_line(monkeypatch, tmp_path):
    script = tmp_path / "live-gateway"
    script.write_text("#!/bin/sh\nsleep 1\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: script)
    )
    assert mod._launchd_live_worktree() is None


def test_launchd_live_worktree_resolves_venv_grandparent(monkeypatch, tmp_path):
    checkout = tmp_path / "wt-feat"
    exe = checkout / ".venv" / "bin" / "kirocrew"
    script = tmp_path / "live-gateway"
    script.write_text(f"#!/bin/sh\nexec '{exe}' gateway\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: script)
    )
    assert mod._launchd_live_worktree() == str(checkout.resolve())


@pytest.mark.asyncio
async def test_live_worktree_path_uses_launchd_on_darwin(monkeypatch):
    monkeypatch.setattr(mod.live_target, "read_target", lambda: None)
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/launchctl")
    monkeypatch.setattr(mod, "_launchd_live_worktree", lambda: "/checkouts/wt")

    assert await mod._live_worktree_path(fresh=True) == "/checkouts/wt"


@pytest.mark.asyncio
async def test_live_worktree_path_none_without_systemd(monkeypatch):
    monkeypatch.setattr(mod.live_target, "read_target", lambda: None)
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(side_effect=RuntimeError))

    assert await mod._live_worktree_path(fresh=True) is None


@pytest.mark.asyncio
async def test_live_worktree_path_falls_back_to_execstart(monkeypatch):
    """An older unit with no WorkingDirectory is read off ExecStart's path=."""
    # A fabricated, space-free path: the regex the product uses truncates at the
    # first space, so a tmp_path containing one would make this assert the wrong
    # thing on some runners.
    checkout = Path("/opt/kirocrew-checkouts/co")
    exe = checkout / ".venv" / "bin" / "kirocrew"
    monkeypatch.setattr(mod.live_target, "read_target", lambda: None)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/systemctl")
    _run_cmd_queue(
        monkeypatch,
        [
            (0, "\n", ""),
            (0, f"ExecStart={{ path={exe} ; argv[]=... }}", ""),
        ],
    )

    assert await mod._live_worktree_path(fresh=True) == str(checkout.resolve())


@pytest.mark.asyncio
async def test_live_worktree_path_prefers_working_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.live_target, "read_target", lambda: None)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/systemctl")
    seen = _run_cmd_queue(monkeypatch, [(0, f"{tmp_path}\n", "")])

    assert await mod._live_worktree_path(fresh=True) == str(tmp_path.resolve())
    # The ExecStart fallback is not consulted when WorkingDirectory answers.
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_live_worktree_path_serves_cache_until_ttl(monkeypatch):
    """Only ``fresh=True`` bypasses the display cache."""
    monkeypatch.setattr(mod, "_LIVE_WORKTREE", "/cached")
    monkeypatch.setattr(mod, "_LIVE_CHECK_AT", mod.time.monotonic())
    monkeypatch.setattr(mod.live_target, "read_target", lambda: None)
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(side_effect=RuntimeError))

    assert await mod._live_worktree_path() == "/cached"


# --------------------------------------------------------------------------
# _find_worktree_by_path
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_find_worktree_by_path_rejects_empty(monkeypatch):
    monkeypatch.setattr(mod, "_discover_worktrees", AsyncMock(side_effect=RuntimeError))
    target, err = await mod._find_worktree_by_path("")
    assert target is None
    assert err == "'path' must be a non-empty string"


@pytest.mark.asyncio
async def test_find_worktree_by_path_rejects_unresolvable(monkeypatch):
    """A NUL byte cannot be resolved: reported as invalid, never enumerated."""
    monkeypatch.setattr(mod, "_discover_worktrees", AsyncMock(side_effect=RuntimeError))
    target, err = await mod._find_worktree_by_path("/wt/\x00bad")
    assert target is None
    assert err is not None and err.startswith("invalid path:")


@pytest.mark.asyncio
async def test_find_worktree_by_path_matches_known_worktree(monkeypatch, tmp_path):
    wt = {"name": "feat", "path": str(tmp_path)}
    monkeypatch.setattr(mod, "_discover_worktrees", AsyncMock(return_value=[wt]))
    assert await mod._find_worktree_by_path(str(tmp_path)) == (wt, None)


@pytest.mark.asyncio
async def test_find_worktree_by_path_refuses_unknown_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mod, "_discover_worktrees",
        AsyncMock(return_value=[{"name": "feat", "path": str(tmp_path / "other")}]),
    )
    target, err = await mod._find_worktree_by_path(str(tmp_path / "mine"))
    assert target is None
    assert err is not None and err.startswith("path is not a known worktree:")


# --------------------------------------------------------------------------
# _dropin_path
# --------------------------------------------------------------------------
def test_dropin_path_honours_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    got = mod._dropin_path()
    assert got.name == "make-live.conf"
    assert got.parent.name == f"{mod._LIVE_GATEWAY_UNIT}.d"
    assert got.is_relative_to(tmp_path / "xdg")


def test_dropin_path_falls_back_to_home_config(monkeypatch, tmp_path):
    home = tmp_path / "h"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert mod._dropin_path() == (
        home / ".config" / "systemd" / "user"
        / f"{mod._LIVE_GATEWAY_UNIT}.d" / "make-live.conf"
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
    monkeypatch.setattr(mod, "_MAKE_LIVE_COMMITTED", True)
    monkeypatch.setattr(mod, "_gateway_backend", lambda: pytest.fail("must not probe"))

    out = await mod._restart_gateway()

    assert out["ok"] is False
    assert "Make Live cutover is in progress" in out["error"]


@pytest.mark.asyncio
async def test_restart_gateway_refuses_while_make_live_lock_held(monkeypatch):
    lock = asyncio.Lock()
    monkeypatch.setattr(mod, "_MAKE_LIVE_LOCK", lock)
    monkeypatch.setattr(mod, "_gateway_backend", lambda: pytest.fail("must not probe"))

    async with lock:
        out = await mod._restart_gateway()

    assert out["ok"] is False
    assert "Make Live cutover is in progress" in out["error"]


@pytest.mark.asyncio
async def test_restart_gateway_uses_service_backend(monkeypatch):
    svc = _FakeBackend(active=True, ok=True)
    monkeypatch.setattr(mod, "_gateway_backend", lambda: svc)
    monkeypatch.setattr(mod, "_gateway_start_id", AsyncMock(return_value="stamp-1"))

    out = await mod._restart_gateway()

    assert out == {"ok": True, "start_id": "stamp-1"}
    assert svc.restarts == 1
    # Latched so a second restart cannot race the pending one.
    assert mod._MAKE_LIVE_COMMITTED is True


@pytest.mark.asyncio
async def test_restart_gateway_reports_service_failure_without_latching(monkeypatch):
    svc = _FakeBackend(active=True, ok=False, err="Job failed")
    monkeypatch.setattr(mod, "_gateway_backend", lambda: svc)
    monkeypatch.setattr(mod, "_gateway_start_id", AsyncMock(return_value=None))

    out = await mod._restart_gateway()

    assert out == {"ok": False, "error": "Job failed"}
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_restart_gateway_falls_back_to_foreground(monkeypatch):
    """No drivable manager: the detached foreground respawn is the last resort."""
    monkeypatch.setattr(mod, "_gateway_backend", lambda: None)
    monkeypatch.setattr(mod, "_live_user_unit_status", AsyncMock(return_value="no_user_unit"))
    fg = _FakeBackend(ok=True)
    monkeypatch.setattr(mod, "_foreground_backend", lambda: fg)
    monkeypatch.setattr(mod, "_gateway_start_id", AsyncMock(return_value="pid-77"))

    out = await mod._restart_gateway()

    assert out == {"ok": True, "start_id": "pid-77"}
    assert fg.restarts == 1


@pytest.mark.asyncio
async def test_restart_gateway_reports_foreground_failure(monkeypatch):
    monkeypatch.setattr(mod, "_gateway_backend", lambda: _FakeBackend(active=False))
    monkeypatch.setattr(mod, "_live_user_unit_status", AsyncMock(return_value="no_agent"))
    monkeypatch.setattr(
        mod, "_foreground_backend", lambda: _FakeBackend(ok=False, err="no marker")
    )
    monkeypatch.setattr(mod, "_gateway_start_id", AsyncMock(return_value=None))

    out = await mod._restart_gateway()

    assert out == {"ok": False, "error": "no marker"}
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_restart_gateway_refuses_confined_status(monkeypatch):
    """A mis-set-up manager keeps its named remedy instead of a blind respawn."""
    monkeypatch.setattr(mod, "_gateway_backend", lambda: _FakeBackend(active=False))
    monkeypatch.setattr(
        mod, "_live_user_unit_status", AsyncMock(return_value="user_unit_inactive")
    )
    monkeypatch.setattr(mod, "_foreground_backend", lambda: pytest.fail("not eligible"))

    out = await mod._restart_gateway()

    assert out == {"ok": False, "error": "gateway is not running as a user service"}


@pytest.mark.asyncio
async def test_restart_gateway_handler_returns_result(monkeypatch):
    monkeypatch.setattr(mod, "_sel", lambda: _NullSel())
    monkeypatch.setattr(mod, "_restart_gateway", AsyncMock(return_value={"ok": True}))

    request = make_mocked_request("POST", "/api/restart-gateway")
    resp = await mod.api_dev_fleet_restart_gateway(request)

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
    monkeypatch.setattr(mod, "_sel", lambda: _NullSel())
    monkeypatch.setattr(mod, "_make_live", AsyncMock(side_effect=RuntimeError))

    resp = await mod.api_dev_fleet_make_live(_body_request(b"{not json"))

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid JSON body"}


@pytest.mark.asyncio
async def test_make_live_handler_requires_path_string(monkeypatch):
    monkeypatch.setattr(mod, "_sel", lambda: _NullSel())
    monkeypatch.setattr(mod, "_make_live", AsyncMock(side_effect=RuntimeError))
    raw = json.dumps({"path": 12}).encode()

    resp = await mod.api_dev_fleet_make_live(_body_request(raw))

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "'path' must be a non-empty string"}


@pytest.mark.asyncio
async def test_make_live_handler_validates_dry_run_type(monkeypatch):
    monkeypatch.setattr(mod, "_sel", lambda: _NullSel())
    monkeypatch.setattr(mod, "_make_live", AsyncMock(side_effect=RuntimeError))
    raw = json.dumps({"path": "/wt/feat", "dry_run": "yes"}).encode()

    resp = await mod.api_dev_fleet_make_live(_body_request(raw))

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "dry_run must be a boolean"}


@pytest.mark.asyncio
async def test_make_live_handler_passes_dry_run_through(monkeypatch):
    monkeypatch.setattr(mod, "_sel", lambda: _NullSel())
    make_live = AsyncMock(return_value={"ok": True, "dry_run": True})
    monkeypatch.setattr(mod, "_make_live", make_live)
    raw = json.dumps({"path": "/wt/feat", "dry_run": True}).encode()

    resp = await mod.api_dev_fleet_make_live(_body_request(raw))

    assert resp.status == 200
    make_live.assert_awaited_once_with("/wt/feat", True)


@pytest.mark.asyncio
async def test_make_live_refuses_missing_worktree_path(monkeypatch, tmp_path):
    """A known worktree whose directory is gone is refused before any mutation."""
    gone = tmp_path / "removed"
    monkeypatch.setattr(
        mod, "_find_worktree_by_path",
        AsyncMock(return_value=({"name": "removed", "path": str(gone)}, None)),
    )
    monkeypatch.setattr(mod, "_in_pod", lambda: pytest.fail("checked too late"))

    out = await mod._make_live(str(gone))

    assert out["ok"] is False
    assert out["code"] == "missing_path"


def test_kill_tree_sync_kills_descendants_first(monkeypatch):
    """Descendants are enumerated before the group kill erases their PPIDs."""
    order: list[str] = []
    monkeypatch.setattr(
        platform_compat, "process_descendants",
        lambda pid: (order.append(f"enum:{pid}"), [11, 12])[1],
    )

    def _kill(pid: int) -> None:
        order.append(f"kill:{pid}")
        if pid == 11:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(platform_compat, "kill_process_tree", _kill)

    mod._kill_tree_sync(7)

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

    mod._kill_tree_sync(9)

    assert killed == [9, 21]


# --------------------------------------------------------------------------
# pod config + request-body validation
# --------------------------------------------------------------------------
def test_load_cfg_none_when_pod_config_unloadable(monkeypatch):
    """A pod config that will not load degrades to None, never an exception."""
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)

    class _Boom:
        @staticmethod
        def load():
            raise RuntimeError("no pods dir")

    monkeypatch.setattr(mod, "PodConfig", _Boom, raising=False)
    assert mod._load_cfg() is None


def test_load_cfg_none_when_pods_unavailable(monkeypatch):
    monkeypatch.setattr(mod, "_POD_AVAILABLE", False)
    assert mod._load_cfg() is None


@pytest.mark.asyncio
async def test_worktree_remove_handler_rejects_unparseable_body(monkeypatch):
    monkeypatch.setattr(mod, "_sel", lambda: _NullSel())
    monkeypatch.setattr(mod, "_worktree_remove", AsyncMock(side_effect=RuntimeError))

    resp = await mod.api_dev_fleet_worktree_remove(_body_request(b"[1, 2]"))

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "body must be an object"}


@pytest.mark.asyncio
async def test_pod_name_action_rejects_unparseable_body(monkeypatch):
    action = AsyncMock(side_effect=RuntimeError)
    resp = await mod._pod_name_action(_body_request(b"nope"), action)

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "invalid JSON body"}
    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_pod_name_action_requires_known_worktree(monkeypatch):
    monkeypatch.setattr(
        mod, "_find_worktree", AsyncMock(return_value=(None, "no such worktree"))
    )
    action = AsyncMock(side_effect=RuntimeError)

    resp = await mod._pod_name_action(_body_request(b'{"name": "ghost"}'), action)

    assert resp.status == 400
    assert json.loads(resp.text) == {"error": "no such worktree"}
    action.assert_not_awaited()


# --------------------------------------------------------------------------
# service drivability probes
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gateway_service_reason_none_when_drivable(monkeypatch):
    monkeypatch.setattr(mod, "_gateway_service_active", AsyncMock(return_value=True))
    assert await mod._gateway_service_reason() is None


@pytest.mark.asyncio
async def test_gateway_service_reason_appends_unknown_checkout_hint(monkeypatch):
    """An unattributable gateway gets the extra Pull+Build caveat."""
    monkeypatch.setattr(mod, "_gateway_service_active", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "_live_user_unit_status", AsyncMock(return_value="no_agent"))
    monkeypatch.setattr(mod, "_live_worktree_path", AsyncMock(return_value=None))

    reason = await mod._gateway_service_reason()

    assert reason is not None
    assert "does not belong to any known worktree" in reason


@pytest.mark.asyncio
async def test_gateway_service_reason_omits_hint_for_known_checkout(monkeypatch):
    monkeypatch.setattr(mod, "_gateway_service_active", AsyncMock(return_value=False))
    monkeypatch.setattr(
        mod, "_live_user_unit_status", AsyncMock(return_value="user_unit_inactive")
    )
    monkeypatch.setattr(mod, "_live_worktree_path", AsyncMock(return_value="/co"))

    reason = await mod._gateway_service_reason()

    assert reason is not None
    assert "does not belong to any known worktree" not in reason


@pytest.mark.asyncio
async def test_gateway_service_active_accepts_foreground_backend(monkeypatch):
    """With no drivable manager, an unconfined foreground backend still counts."""
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(mod, "_gateway_backend", lambda: None)
    monkeypatch.setattr(mod, "_live_user_unit_status", AsyncMock(return_value="no_systemd"))
    monkeypatch.setattr(mod, "_foreground_backend", lambda: _FakeBackend())

    assert await mod._gateway_service_active() is True
    assert mod._GATEWAY_SERVICE_ACTIVE is True


@pytest.mark.asyncio
async def test_gateway_service_active_false_when_foreground_confined(monkeypatch):
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_ACTIVE", None)
    monkeypatch.setattr(mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0)
    monkeypatch.setattr(mod, "_gateway_backend", lambda: _FakeBackend(active=False))
    monkeypatch.setattr(
        mod, "_live_user_unit_status", AsyncMock(return_value="user_unit_inactive")
    )
    monkeypatch.setattr(mod, "_foreground_backend", lambda: pytest.fail("not eligible"))

    assert await mod._gateway_service_active() is False
