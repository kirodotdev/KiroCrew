"""Route tests: the app's git invocations.

Every git run is audited to the security event log before it spawns and is
refused when that audit cannot be written; a missing git or a refusing sandbox
degrades instead of failing the request; a cancelled or failing git is killed on
every exceptional exit; and worktree creation refuses an existing path.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes, routes_source

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _make_client,
    _never_touch_the_real_state,
)

# ── atomic state writes and git auditing ─────────────────────────────────────


def _stub_spawn(monkeypatch):
    """Replace the sandboxed argv with a trivial no-op binary. The host running
    the suite may have no sandbox backend available, and this test is about the
    AUDIT contract, not about sandboxing or git itself."""
    monkeypatch.setattr(
        routes, "sandboxed_spawn_argv", lambda argv: ([sys.executable, "-c", ""], None, "")
    )


@pytest.mark.asyncio
async def test_git_invocations_are_audited_to_sel(tmp_path, monkeypatch):
    """The reported gap: this app spawns git against the user's repository, and a
    worktree create/remove left no tool-invocation trail — only app-level
    spec_worktree_* entries, which record neither what git ran nor whether it
    failed. Every invocation and outcome now emits log_tool_invocation."""
    events: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            events.append(kw)

        def log_api_access(self, **kw):
            pass

    monkeypatch.setattr(routes, "sel", lambda: _Sel())
    _stub_spawn(monkeypatch)
    rc, _out, _err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")

    outcomes = [e["outcome"] for e in events]
    assert "invoked" in outcomes, "no invocation event recorded"
    assert outcomes[-1] in ("success", "failure"), f"no outcome event: {outcomes}"
    assert all(e["tool_name"] == "git" for e in events)
    assert events[0]["metadata"]["subcommand"] == "rev-parse"
    # Coarse by design: the full argv is never logged (a branch name derives from
    # user input), only the subcommand + working directory.
    assert "--show-toplevel" not in json.dumps(events)
    assert rc == 0


@pytest.mark.asyncio
async def test_git_outcome_audit_failure_does_not_break_the_command(tmp_path, monkeypatch):
    """An OUTCOME audit failure must not break a command that already ran.

    Narrowed from "auditing must never break the flow it audits": the INVOCATION
    event is now a precondition for the spawn (a swallowed failure meant git ran on
    the user's repository with no tool-invocation record at all). Losing the
    outcome event afterwards is different — the process has already run, and
    turning that into an error would report a successful command as failed.
    """
    calls: list[str] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            calls.append(kw.get("outcome", ""))
            if kw.get("outcome") != "invoked":
                raise RuntimeError("sel down")

        def log_api_access(self, **kw):
            pass

    monkeypatch.setattr(routes, "sel", lambda: _Sel())
    _stub_spawn(monkeypatch)
    rc, _out, _err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")
    assert rc == 0
    assert calls[0] == "invoked" and len(calls) > 1, calls


# ── recents and settings IO off the loop; missing git degrades ───────────────


@pytest.mark.asyncio
async def test_missing_git_degrades_instead_of_500(tmp_path, monkeypatch):
    """The reported crash: browsing a folder calls _repo_info -> _git, so on a
    host without git the FileNotFoundError propagated and the project picker's
    first request returned HTTP 500. The app is usable without git (the worktree
    option simply isn't offered), so it must degrade."""
    monkeypatch.setattr(
        routes, "sandboxed_spawn_argv", lambda argv: (["/nonexistent/git"], None, "")
    )

    rc, out, err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")
    assert rc == routes._GIT_UNAVAILABLE and out == ""
    assert "git" in err

    # _repo_info must then report "not a git repo" rather than raising.
    assert await routes._repo_info(str(tmp_path)) == {"is_git": False}


@pytest.mark.asyncio
async def test_git_reports_unavailable_when_the_sandbox_refuses(tmp_path, monkeypatch):
    """Same contract when the sandbox cannot build an argv at all — this host has
    no sandbox backend, which is exactly how the suite hits that path."""

    def _boom(_argv):
        raise RuntimeError("sandbox backend unavailable")

    monkeypatch.setattr(routes, "sandboxed_spawn_argv", _boom)
    rc, _out, err = await routes._git(str(tmp_path), "status")
    assert rc == routes._GIT_UNAVAILABLE
    assert "unavailable" in err


# ── no async function touches the filesystem inline ──────────────────────────


@pytest.mark.asyncio
async def test_create_worktree_still_refuses_an_existing_path(tmp_path, monkeypatch):
    """Non-vacuous: offloading the probe must not lose the guard — an existing
    sibling path still aborts before git runs."""
    repo = Path(os.path.realpath(tmp_path)) / "repo"
    repo.mkdir()
    (repo.parent / "repo-wt-taken").mkdir()

    ran: list[tuple] = []

    async def _git(cwd, *args):
        ran.append(args)
        return 0, "", ""

    monkeypatch.setattr(routes, "_git", _git)
    monkeypatch.setattr(routes, "_repo_info", lambda p: _async_value({"default_base": "main"}))

    out = await routes._create_worktree(str(repo), "taken")
    assert isinstance(out, str) and "already exists" in out
    assert ran == [], "git ran despite the path already existing"


async def _async_value(v):
    return v


# ── a minted slot key survives the commit; git needs its audit ───────────────


@pytest.mark.asyncio
async def test_git_refuses_to_run_when_the_invocation_cannot_be_audited(tmp_path, monkeypatch):
    """The reported gap: _audit_tool returned silently when SEL was missing or its
    log unwritable, so this app spawned git on the user's repository with no
    tool-invocation record. The invocation event is a precondition for the spawn."""
    spawned: list[list[str]] = []

    def _spawn_prep(argv):
        spawned.append(argv)
        raise AssertionError("git was spawned without an audit record")

    monkeypatch.setattr(routes, "_prepare_git_spawn", _spawn_prep)
    monkeypatch.setattr(routes, "sel", None)

    rc, out, err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")

    assert rc == routes._GIT_UNAVAILABLE
    assert "audit" in err
    assert spawned == []

    # Outcome events stay best-effort: a failure there must not turn a command
    # that already ran into an error.
    src = inspect.getsource(routes._git)
    gate = 'await asyncio.to_thread(_audit_tool, "invoked", subcommand, cwd, critical=True)'
    assert gate in src, "the invocation audit is not the critical, off-loop form"
    assert src.index(gate) < src.index("_prepare_git_spawn"), "git is prepared before the audit"
    # Outcome events stay queued and best-effort: they must NOT be critical, or a
    # failed log write would turn a command that already ran into an error.
    for outcome in ('"error"', '"success" if rc == 0 else "failure"'):
        for line in src.splitlines():
            if f"_audit_tool({outcome}" in line:
                assert "critical" not in line, f"outcome audit made critical: {line.strip()}"
    assert src.count('_audit_tool("error"') >= 1


# ── git refuses when its invocation cannot be audited ────────────────────────


@pytest.mark.asyncio
async def test_git_refuses_when_only_the_durable_audit_write_fails(tmp_path, monkeypatch):
    """The default log path ENQUEUES the event and a background writer flushes it, so
    `_audit_tool` returning True proves only that the enqueue did not raise -- the
    record can still be dropped when the log is unwritable, leaving git unaudited. The
    invocation event is written with `critical=True`, which raises on a filesystem
    failure.

    The stub models exactly that asymmetry: a queued (non-critical) call succeeds, a
    critical one raises. A gate that never asked for durability would pass."""
    spawned: list[list[str]] = []
    calls: list[dict] = []

    def _spawn_prep(argv):
        spawned.append(argv)
        raise AssertionError("git was spawned without a durable audit record")

    class _Sel:
        def log_tool_invocation(self, **kw):
            calls.append(kw)
            if kw.get("critical"):
                raise OSError("audit log is unwritable")

    monkeypatch.setattr(routes, "_prepare_git_spawn", _spawn_prep)
    monkeypatch.setattr(routes, "sel", lambda: _Sel())

    rc, _out, err = await routes._git(str(tmp_path), "rev-parse", "--show-toplevel")

    assert rc == routes._GIT_UNAVAILABLE, rc
    assert "audit" in err
    assert spawned == [], "git ran despite the audit write failing"
    assert (
        calls and calls[0]["critical"] is True
    ), f"the invocation audit did not ask for a durable write: {calls}"


def test_only_the_invocation_audit_is_critical():
    """Source guard on the calls: the precondition event is durable, the outcome
    events stay queued. Making the outcomes critical would let a failed log write
    turn a command that already ran into an error."""
    src = inspect.getsource(routes._git)
    critical_lines = [ln.strip() for ln in src.splitlines() if "critical=True" in ln]
    assert len(critical_lines) == 1, f"expected exactly one critical audit: {critical_lines}"
    assert '"invoked"' in critical_lines[0], critical_lines[0]
    # And it is awaited off the loop, because a critical write is synchronous I/O.
    assert "asyncio.to_thread(_audit_tool" in critical_lines[0], critical_lines[0]


def test_audit_helper_defaults_to_queued():
    """`_audit_tool` must keep its non-critical default: every other caller of it is
    an outcome event, and flipping the default would make them all blocking."""
    sig = inspect.signature(routes._audit_tool)
    assert sig.parameters["critical"].default is False


# ── non-finite timestamps; git is killed on every exceptional exit ───────────


@pytest.mark.asyncio
async def test_cancelled_git_is_killed_before_the_handler_unwinds(tmp_path, monkeypatch):
    """The reported leak: only `await proc.communicate()` tied git's lifetime to the
    request. Cancel the handler mid-spawn and git ran on -- `worktree add` would
    create a worktree and a branch after the request that asked for it was gone."""
    client = _make_client(monkeypatch, tmp_path)
    killed: list[str] = []
    parked = asyncio.Event()

    class _HangingProc:
        returncode = None

        async def communicate(self):
            # Signal from INSIDE the await the cancellation has to land on, rather
            # than letting the test guess with a sleep. _git makes two to_thread
            # hops before this point and _prepare_git_spawn forks a sandbox probe
            # on first use, so any fixed delay races them: under load the cancel
            # arrives before the spawn, proc is still None, and the test asserts
            # against a teardown that never had a process to kill.
            parked.set()
            # flake-ok: the fake git hangs here until the test cancels it; nothing waits on it
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the fake git hangs until the test cancels it; nothing waits on this sleep
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        def kill(self):
            killed.append("kill")
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def _fake_spawn(*argv, **kwargs):
        return _HangingProc()

    monkeypatch.setattr(routes, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(routes, "_audit_tool", lambda *a, **k: True)
    # Also stub the sandbox preparation. _git makes TWO to_thread hops before the
    # spawn, and this one really probes the sandbox host and writes a scrubbed-env
    # temp file -- work that can fork. Left real, it sits inside the window the
    # wait below bounds, so on a heavily parallel shard the test failed on the
    # timeout rather than on the behaviour it exists to check. The assertion is
    # only about the process being killed, so sandbox preparation is setup here,
    # not subject: stubbing it makes the window depend on scheduling alone.
    monkeypatch.setattr(routes, "_prepare_git_spawn", lambda argv: (list(argv), {}, None))

    await client.start_server()
    try:
        task = asyncio.ensure_future(routes._git(str(tmp_path), "worktree", "add", "x"))
        await asyncio.wait_for(parked.wait(), timeout=30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.close()

    assert killed == ["kill"], "the spawned git process outlived the cancelled request"


@pytest.mark.asyncio
async def test_halt_git_tolerates_an_already_dead_process(tmp_path):
    """The teardown runs on every exceptional exit, including ones where the process
    never started or has already been reaped -- it must not raise there and mask the
    original exception."""

    class _Gone:
        returncode = None

        def kill(self):
            raise ProcessLookupError()

        async def wait(self):  # pragma: no cover - must not be reached
            raise AssertionError("waited on a process that was already gone")

    await routes._halt_git(None, "status")  # never spawned
    await routes._halt_git(_Gone(), "status")  # died between the check and the kill

    class _Reaped:
        returncode = 0

        def kill(self):  # pragma: no cover - must not be reached
            raise AssertionError("killed an already-reaped process")

        async def wait(self):  # pragma: no cover
            raise AssertionError("waited on an already-reaped process")

    await routes._halt_git(_Reaped(), "status")


def test_git_kills_the_process_on_every_exceptional_exit():
    """Source guard: the teardown is wired into the arm that re-raises, so a future
    exception class added to _git cannot silently reintroduce the orphan. Asserts on
    the CALL, not on a comment."""
    tree = ast.parse(routes_source())
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_git"
    )
    reraise_arms = [
        handler
        for handler in (h for t in ast.walk(target) if isinstance(t, ast.Try) for h in t.handlers)
        if any(isinstance(n, ast.Raise) and n.exc is None for n in ast.walk(handler))
    ]
    assert reraise_arms, "_git no longer re-raises; this guard needs rewriting"
    for arm in reraise_arms:
        calls = [
            n.func.id
            for n in ast.walk(arm)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "_halt_git" in calls, (
            "an arm of _git re-raises without calling _halt_git: a cancelled "
            "worktree add would leave git running against the user's repository"
        )
