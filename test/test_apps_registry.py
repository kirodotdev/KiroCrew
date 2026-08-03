"""Regression tests for subprocess-timeout remediation in apps/registry.py.

These cover the audit findings that timed-out child subprocesses were left
un-reaped (zombie/leak) or, for the install-script path, only sent a single
SIGTERM with no reap and no SIGKILL escalation:

  * git-clone manifest fetch  -> _communicate_with_timeout (tree-kill + reap)
  * external registry index    -> _communicate_with_timeout (tree-kill + reap)
  * list_registry detect probe -> _communicate_with_timeout (tree-kill + reap)
  * install detect probe       -> _communicate_with_timeout (tree-kill + reap)
  * install-script timeout      -> _kill_process_group (reap + SIGKILL)

``_communicate_with_timeout`` now signals the child's whole process group
(``platform_compat.kill_process_tree_async``) instead of ``proc.kill()``-ing
only the immediate child, so a hung ``git clone``/``/bin/sh -c <probe>`` cannot
leave re-parented grandchildren running. Each spawn feeding it is started with
``start_new_session`` so the group signal targets the child's own group.

This file lives in ``test/`` (not ``tests/``) so the ``setup.cfg``
``testpaths = test transfer`` gate — and therefore CI — actually collects it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import MagicMock

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps import registry


@pytest.fixture(autouse=True)
def _explicit_registry_execution_admission(monkeypatch):
    """These tests must reach admitted registry subprocess paths."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


# A portable long-lived child: sleeps well past any test timeout without
# relying on POSIX-only binaries (``sleep``/``bash`` are absent on native
# Windows, where they would fail collection with FileNotFoundError).
_SLEEP_SCRIPT = "import time; time.sleep(60)"
# A portable child that ignores SIGTERM so the group kill must escalate to
# SIGKILL to stop it. SIGTERM-ignore + SIGKILL escalation is POSIX signal
# semantics, so tests using this are guarded with skipif(not IS_POSIX).
_SIGTERM_IGNORE_SCRIPT = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "\nwhile True: time.sleep(0.2)"
)


class _TimeoutProc:
    """Fake subprocess whose ``communicate()`` times out.

    Lets us exercise the timeout branch instantly (no real long-running
    process) while recording whether the branch killed and reaped the child.
    """

    def __init__(self) -> None:
        self.pid = 987654
        self.returncode: int | None = None
        self.kill_calls = 0
        self.wait_calls = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        raise asyncio.TimeoutError

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


def _record_tree_kill(monkeypatch) -> list[int]:
    """Patch the process-tree killer to record the pids it was asked to kill.

    Returns the list that each ``_communicate_with_timeout`` timeout appends
    its ``proc.pid`` to — proving the whole group was signalled rather than a
    single ``proc.kill()``.
    """
    killed: list[int] = []

    async def _fake_tree_kill(pid, sig):
        killed.append(pid)
        return True

    monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _fake_tree_kill)
    return killed


# --------------------------------------------------------------------------
# Shared helper: _communicate_with_timeout (mechanism behind bugs 1, 2a, 2b)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_communicate_with_timeout_kills_and_reaps_real_subprocess():
    """A hung child (its own session leader) must be group-killed AND reaped."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _SLEEP_SCRIPT,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    pid = proc.pid
    with pytest.raises(asyncio.TimeoutError):
        await registry._communicate_with_timeout(proc, timeout=0.2)

    # Reaped: returncode is populated, so the child is not a zombie.
    assert proc.returncode is not None
    # And the process is genuinely gone (portable liveness check — never the
    # prohibited raw ``os.kill(pid, 0)``, which kills on Windows PID reuse).
    assert not platform_compat.pid_exists(pid)


@pytest.mark.asyncio
async def test_communicate_with_timeout_kills_whole_process_tree(monkeypatch):
    """The timeout path signals the child's whole group, not just proc.kill()."""
    proc = _TimeoutProc()
    killed: list[tuple[int, int]] = []

    async def _fake_tree_kill(pid, sig):
        killed.append((pid, sig))
        return True

    monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _fake_tree_kill)
    with pytest.raises(asyncio.TimeoutError):
        await registry._communicate_with_timeout(proc, timeout=0.01)

    # Whole-tree kill was invoked with the child's pid + SIGKILL ...
    assert killed == [(proc.pid, registry.platform_compat.SIGKILL)]
    # ... the child was reaped ...
    assert proc.wait_calls == 1
    # ... and the single-process fallback was NOT needed.
    assert proc.kill_calls == 0


@pytest.mark.asyncio
async def test_communicate_with_timeout_falls_back_when_group_kill_fails(monkeypatch):
    """If the group kill raises OSError, fall back to a pid-scoped kill + reap."""
    proc = _TimeoutProc()

    async def _boom(pid, sig):
        raise ProcessLookupError  # subclass of OSError

    monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _boom)
    with pytest.raises(asyncio.TimeoutError):
        await registry._communicate_with_timeout(proc, timeout=0.01)

    assert proc.kill_calls == 1
    assert proc.wait_calls == 1


@pytest.fixture(autouse=True)
def unsandboxed_spawn(monkeypatch):
    """Decouple this module's timeout/reap tests from the host's sandbox capability.

    Every test here asserts process-group signalling and reaping, and they mock
    ``create_subprocess_exec``, so no child process ever actually runs. What they
    must not depend on is whether THIS host can build a namespace sandbox: a CI
    runner with ``kernel.apparmor_restrict_unprivileged_userns=1`` legitimately
    cannot, and ``wrap_argv`` then fail-closes by design. These tests previously
    passed only because the capability probe returned a false positive on such
    hosts. Autouse because the coupling is a property of the whole module, not of
    individual tests. Sandbox construction is covered by ``test_sandbox_*.py``.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)


# --------------------------------------------------------------------------
# Bug 1 — git-clone manifest fetch reaps the clone tree on timeout
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_app_manifest_reaps_clone_tree_on_timeout(monkeypatch):
    proc = _TimeoutProc()
    killed = _record_tree_kill(monkeypatch)

    async def _fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    # The SSRF host-trust gate short-circuits untrusted hosts before the clone
    # spawns; this test targets the timeout-reap path AFTER the gate admits the
    # host, so treat the test host as trusted.
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)

    result = await registry._fetch_app_manifest(
        repo="https://example.com/demo.git",
        branch="main",
        git_url="https://example.com/demo.git",
    )

    # Timeout is swallowed (listing must never crash) ...
    assert result is None
    # ... but the clone's whole process group was killed and the child reaped.
    assert killed == [proc.pid]
    assert proc.wait_calls == 1


# --------------------------------------------------------------------------
# Bug 2a — list_registry detectInstalled probe reaps the tree on timeout
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_registry_reaps_detect_probe_tree_on_timeout(monkeypatch):
    entry = {"name": "probeapp", "repo": "x", "detectInstalled": "true"}
    monkeypatch.setattr(registry, "_load_registry_file", lambda: [entry])

    async def _no_external():
        return []

    monkeypatch.setattr(registry, "_load_external_registries", _no_external)
    monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

    async def _resolve(e):
        return e

    monkeypatch.setattr(registry, "_resolve_manifest", _resolve)
    monkeypatch.setattr(
        registry, "_enrich_with_install_status", lambda e, m, d: {"detected": sorted(d)}
    )

    proc = _TimeoutProc()
    killed = _record_tree_kill(monkeypatch)

    async def _fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    await registry.list_registry()

    assert killed == [proc.pid]
    assert proc.wait_calls == 1


# --------------------------------------------------------------------------
# Bug 2b — install_from_registry detect probe reaps the tree on timeout
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_install_from_registry_reaps_detect_probe_tree_on_timeout(monkeypatch):
    entry = {
        "name": "demoapp",
        "repo": "https://example.com/demo.git",
        "detectInstalled": "true",
    }
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args, **kwargs):
        return {}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    proc = _TimeoutProc()
    killed = _record_tree_kill(monkeypatch)

    async def _fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    # Stop right after the detect probe by failing the build fast.
    async def _fake_build(*args, **kwargs):
        return {"ok": False, "error": "stop-after-detect"}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_build)

    await registry.install_from_registry("demoapp")

    assert killed == [proc.pid]
    assert proc.wait_calls == 1


# --------------------------------------------------------------------------
# Bug 3 — install-script timeout: reap + SIGKILL escalation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="SIGTERM-ignore + SIGKILL escalation is POSIX signal semantics",
)
async def test_kill_process_group_reaps_and_escalates_to_sigkill(monkeypatch):
    """A process group that ignores SIGTERM must be escalated to SIGKILL and reaped."""
    monkeypatch.setattr(registry, "_KILL_GRACE_PERIOD", 0.3)

    # Child ignores SIGTERM and keeps running -> only SIGKILL can stop it.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _SIGTERM_IGNORE_SCRIPT,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    pid = proc.pid

    await registry._kill_process_group(proc)

    # Reaped after escalation.
    assert proc.returncode is not None
    # Portable liveness check — never the prohibited raw ``os.kill(pid, 0)``.
    assert not platform_compat.pid_exists(pid)


@pytest.mark.asyncio
async def test_install_script_timeout_routes_through_kill_process_group(monkeypatch, tmp_path):
    """On install-script timeout the code must call _kill_process_group (reap +
    SIGKILL escalation), not the old fire-and-forget single SIGTERM."""
    entry = {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"}
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args, **kwargs):
        return {}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    # Cloned app source carries an install script.
    (tmp_path / "app.json").write_text(
        json.dumps({"setup": {"onInstall": "sleep 999"}}), encoding="utf-8"
    )

    async def _fake_build(git_url, name, log_lines, branch="main", **kwargs):
        return {"ok": True, "pkg_dir": tmp_path}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_build)

    kpg_calls: list[object] = []

    async def _fake_kpg(proc):
        kpg_calls.append(proc)
        proc.returncode = -9  # emulate reap

    monkeypatch.setattr(registry, "_kill_process_group", _fake_kpg)

    proc = _TimeoutProc()

    async def _fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "timed out" in result["error"]
    # The timeout path routed through the reaping/escalating helper.
    assert kpg_calls == [proc]


# ---------------------------------------------------------------------------
# Git-install build step: the interpreter, and where the build runs.
#
# Both properties below were broken and NEITHER had a test, which is why they
# survived — and both fail SILENTLY, reporting a successful install that installed
# nothing the gateway can import.
# ---------------------------------------------------------------------------


def _build_cmds_for(tmp_path, monkeypatch, files: dict[str, str]) -> list[list[str]]:
    """Run ``_run_app_build``'s command planning without executing anything.

    Captures the argv list rather than asserting on side effects: the point of both
    tests is WHICH command would run, and executing a real pip install in a unit test
    would be both slow and environment-dependent.
    """
    for rel, body in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    captured: list[list[str]] = []

    class _EmptyStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Ok:
        returncode = 0
        stdout = _EmptyStdout()

        async def wait(self):
            return 0

        async def communicate(self):
            return (b"", b"")

    async def _fake_exec(*argv, **_kwargs):
        captured.append(list(argv))
        return _Ok()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_exec)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="standard": (list(cmd), None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
    return captured


@pytest.mark.asyncio
async def test_python_build_uses_the_running_interpreter_not_path_pip(tmp_path, monkeypatch):
    """A Python app must install into the interpreter that will IMPORT it.

    ``shutil.which("pip")`` resolves to whatever pip is first on PATH, which is
    routinely NOT the gateway's: ``bin/kirocrew`` execs ``.venv/bin/kirocrew`` without
    putting the venv's ``bin/`` on PATH, and ``service_path()`` prepends
    ``~/.local/bin`` ahead of it.

    The failure mode is silent, which is what made it survive. Measured on a host whose
    first pip was 3.7 and whose gateway venv was 3.12: a *compatible-but-different* pip
    (3.10) reported "Successfully installed", the build reported success, and the package
    landed in ``~/.local/lib/python3.10/site-packages`` — invisible to the gateway, and
    a venv sets ``ENABLE_USER_SITE = False`` so there is no fallback.

    Asserting ``sys.executable`` rather than "not the string 'pip'" so the test states
    the property (install into THIS interpreter) instead of banning one spelling.
    """
    captured = _build_cmds_for(
        tmp_path, monkeypatch, {"pyproject.toml": "[project]\nname='x'\nversion='0'\n"}
    )
    # A PATH pip that is emphatically not us — the old code would have used it.
    monkeypatch.setattr(registry.shutil, "which", lambda name: f"/usr/bin/{name}")

    await registry._run_app_build(tmp_path, "x", [])

    assert captured, "a pyproject.toml must produce a build command"
    argv = captured[0]
    assert argv[0] == sys.executable, f"build must use the running interpreter, got {argv[0]!r}"
    assert argv[1:3] == ["-m", "pip"], f"expected `-m pip`, got {argv[1:3]!r}"


@pytest.mark.asyncio
async def test_a_monorepo_subdirectory_is_built_not_the_clone_root(tmp_path, monkeypatch):
    """The build must run where the package IS, not at the clone root.

    A monorepo registry entry declares ``subdirectory``, and that used to be joined
    only AFTER the build — so the build looked for pyproject.toml at the clone root,
    found none, logged "No build step detected — using source as-is" and returned
    ok=True having installed nothing.
    """
    captured: list = []

    async def _fake_build(build_dir, app_name, log_lines):
        captured.append(build_dir)
        return {"ok": True}

    async def _fake_clone(git_url, branch, pkg_dir, log_lines, *, index_originated=False):
        (pkg_dir / "apps" / "my-tool").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "apps" / "my-tool" / "pyproject.toml").write_text("[project]\n", "utf-8")
        return None

    monkeypatch.setattr(registry, "_run_app_build", _fake_build)
    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_source_dir", lambda name: tmp_path / name)
    (tmp_path / "my-tool").mkdir(parents=True, exist_ok=True)

    await registry._clone_build_app_locked(
        "https://example.invalid/r.git", "my-tool", [], subdirectory="apps/my-tool"
    )

    assert captured, "the build must be attempted"
    assert (
        captured[0].name == "my-tool" and captured[0].parent.name == "apps"
    ), f"build ran in {captured[0]} — expected the declared subdirectory"


@pytest.mark.asyncio
async def test_a_traversing_subdirectory_does_not_choose_the_build_dir(tmp_path, monkeypatch):
    """``subdirectory`` is untrusted index content, so it must not escape the clone.

    Falls back to the clone root rather than failing here: the caller performs its own
    containment check immediately after and returns the precise error, so failing twice
    would only make the message worse.
    """
    captured: list = []

    async def _fake_build(build_dir, app_name, log_lines):
        captured.append(build_dir)
        return {"ok": True}

    async def _fake_clone(git_url, branch, pkg_dir, log_lines, *, index_originated=False):
        pkg_dir.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(registry, "_run_app_build", _fake_build)
    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_source_dir", lambda name: tmp_path / name)

    await registry._clone_build_app_locked(
        "https://example.invalid/r.git", "evil", [], subdirectory="../../etc"
    )

    assert captured == [tmp_path / "evil"], f"escaped the clone root: {captured}"
