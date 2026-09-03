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
import os
import shutil
import stat
import subprocess
import sys
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
        self.communicate_calls = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise asyncio.TimeoutError
        # The reap: pipes drained, exit status collected.
        if self.returncode is None:
            self.returncode = -9
        return b"", b""

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


def _command_git_config(env: dict[str, object]) -> list[tuple[object, object]]:
    """Ordered command-scope Git config pairs captured at a spawn boundary."""
    count = int(env["GIT_CONFIG_COUNT"])
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


def _assert_credential_transport_hardening(
    env: dict[str, object], raw_url: str, public_url: str
) -> None:
    pairs = _command_git_config(env)
    assert (f"url.{raw_url}.insteadOf", public_url) in pairs
    assert pairs[-4:] == [
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
        ("core.askPass", ""),
        ("core.hooksPath", os.devnull),
    ]


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
    # ... the child was reaped by draining pipes via a SECOND communicate(),
    # never a bare wait() that a full pipe could hang (#5989) ...
    assert proc.communicate_calls == 2
    assert proc.wait_calls == 0
    # ... and the helper's pid-scoped kill backs up the group signal.
    assert proc.kill_calls == 1


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
    assert proc.communicate_calls == 2
    assert proc.wait_calls == 0


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
    assert proc.communicate_calls == 2
    assert proc.wait_calls == 0


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
    # Return the entries themselves: list_registry's tail now feeds this
    # result into _apply_trust_fields, which iterates rows as dicts.
    monkeypatch.setattr(
        registry, "_enrich_with_install_status", lambda e, m, d: e
    )

    proc = _TimeoutProc()
    killed = _record_tree_kill(monkeypatch)

    async def _fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    await registry.list_registry()

    assert killed == [proc.pid]
    assert proc.communicate_calls == 2
    assert proc.wait_calls == 0


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
    assert proc.communicate_calls == 2
    assert proc.wait_calls == 0


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
async def test_kill_process_group_escalation_reaps_via_communicate_not_wait(monkeypatch):
    """The SIGKILL escalation reaps by draining pipes via communicate(); a
    bare wait() on a killed child blocked writing into a full pipe would
    hang the app-build timeout path forever (#5989)."""
    monkeypatch.setattr(registry, "_KILL_GRACE_PERIOD", 0.01)
    killed: list[tuple[int, int]] = []

    async def _tree(pid, sig):
        killed.append((pid, sig))
        return True

    monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree)

    class _StubbornProc:
        pid = 987654
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_calls = 0
            self.wait_calls = 0
            self.communicate_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                # The site's own grace wait: SIGTERM is ignored, so this
                # outlives _KILL_GRACE_PERIOD and the escalation fires.
                await asyncio.sleep(3600)
            raise AssertionError("bare wait() used as the post-SIGKILL reap (#5989)")

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            self.returncode = -9
            return b"", b""

        def kill(self) -> None:
            self.kill_calls += 1

    proc = _StubbornProc()
    await registry._kill_process_group(proc)

    pc = registry.platform_compat
    assert killed == [(proc.pid, pc.SIGTERM), (proc.pid, pc.SIGKILL)]
    assert proc.wait_calls == 1  # the grace wait only — never the reap
    assert proc.communicate_calls == 1


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

    # Cloned app source carries an install script. Its manifest must declare the
    # entry's name, or the identity gate refuses before the script ever runs.
    (tmp_path / "app.json").write_text(
        json.dumps({"name": "demoapp", "setup": {"onInstall": "sleep 999"}}),
        encoding="utf-8",
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


# --------------------------------------------------------------------------
# Identity-refusal cleanup + provenance-signer freshness
# --------------------------------------------------------------------------
def _identity_harness(monkeypatch, src, *, cloned_manifest, prefetched=None):
    """Common monkeypatch set for driving install_from_registry to the identity gate."""
    entry = {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"}
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args, **kwargs):
        return prefetched or {}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

    async def _fake_build(git_url, name, log_lines, branch="main", **kwargs):
        # Capture BEFORE materializing, mirroring production's pre-clone
        # snapshot (and its effective-fresh reset after a move-aside).
        preexisted = (src / ".git").is_dir()
        src.mkdir(parents=True, exist_ok=True)
        (src / "app.json").write_text(json.dumps(cloned_manifest), encoding="utf-8")
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": preexisted,
            "_pre_pull_commit": "",
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_build)


@pytest.mark.asyncio
async def test_identity_refusal_preserves_preexisting_checkout(monkeypatch, tmp_path):
    """UPDATE path: a pull that brings a self-renaming manifest is refused
    WITHOUT deleting the installed app's pre-existing source workspace."""
    src = tmp_path / "app-sources" / "demoapp"
    (src / ".git").mkdir(parents=True)  # checkout pre-exists BEFORE the run
    (src / "keep.txt").write_text("user state", encoding="utf-8")
    _identity_harness(monkeypatch, src, cloned_manifest={"name": "renamed-app"})

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    # The workspace survived the refusal.
    assert (src / ".git").is_dir()
    assert (src / "keep.txt").read_text(encoding="utf-8") == "user state"


@pytest.mark.asyncio
async def test_identity_refusal_deletes_fresh_clone(monkeypatch, tmp_path):
    """FRESH install path: a clone created this run leaves no residue on refusal."""
    src = tmp_path / "app-sources" / "demoapp"  # does NOT exist before the run
    _identity_harness(monkeypatch, src, cloned_manifest={"name": "evil-app"})

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    assert not src.exists()


@pytest.mark.asyncio
async def test_provenance_signer_comes_from_cloned_manifest(monkeypatch, tmp_path):
    """The signer persisted as provenance is computed from the identity-checked
    CLONED manifest — never from the pre-clone prefetch, which can be stale
    (signed preview, unsigned pulled commit)."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "9.9.9"},
        prefetched={"name": "demoapp", "version": "1.0.0"},
    )
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "a" * 40)

    signer_calls: list[object] = []

    def _fake_signer(manifest):
        signer_calls.append(manifest)
        return "cloned-signer"

    monkeypatch.setattr(registry, "verified_signer", _fake_signer)

    provenance: dict[str, object] = {}

    def _fake_set_provenance(name, **kwargs):
        provenance.update(kwargs, name=name)

    monkeypatch.setattr(registry, "set_app_provenance", _fake_set_provenance)
    monkeypatch.setattr(registry, "get_app", lambda n: None)
    monkeypatch.setattr(
        registry,
        "install_app",
        lambda path, **kwargs: MagicMock(
            ok=True, name="demoapp", message="done", error=""
        ),
    )

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is True
    # Exactly one signer computation, and it saw the CLONED manifest.
    assert len(signer_calls) == 1
    assert getattr(signer_calls[0], "version", "") == "9.9.9"
    assert provenance["signer"] == "cloned-signer"
    assert provenance["commit"] == "a" * 40


@pytest.mark.asyncio
async def test_install_persists_credential_free_source_provenance(monkeypatch, tmp_path):
    secret = "InstalledSourceSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "1.0.0"},
    )
    monkeypatch.setattr(registry, "_entry_git_url", lambda entry: raw_url)
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "a" * 40)
    monkeypatch.setattr(registry, "verified_signer", lambda manifest: "")
    monkeypatch.setattr(registry, "get_app", lambda name: None)
    installed: dict[str, object] = {}
    provenance: dict[str, object] = {}

    def _install(path):
        # The manager callable contract remains one positional argument; the
        # safe server-resolved coordinate crosses the to_thread boundary in a
        # task-local context rather than in app-controlled manifest data.
        from kiro_crew.apps.manager import _effective_source_repository

        installed["source_repository"] = _effective_source_repository("")
        return MagicMock(ok=True, name="demoapp", message="done", error="")

    monkeypatch.setattr(registry, "install_app", _install)
    monkeypatch.setattr(
        registry,
        "set_app_provenance",
        lambda name, **kwargs: provenance.update(kwargs, name=name),
    )

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is True
    assert installed["source_repository"] == public_url
    assert provenance["url"] == public_url
    assert secret not in str({"installed": installed, "provenance": provenance})


@pytest.mark.asyncio
async def test_registry_fresh_reinstall_checks_retained_startup_before_clone(
    monkeypatch, tmp_path
):
    """Missing metadata must not hide retained old-version startup ownership."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "2.0.0"},
    )
    monkeypatch.setattr(registry, "get_app", lambda _name: None)

    from kiro_crew.apps import hooks_integration

    cleanup_calls: list[tuple[str, bool]] = []

    async def _cleanup(app_name: str, *, bounded: bool) -> bool:
        cleanup_calls.append((app_name, bounded))
        return False

    monkeypatch.setattr(
        hooks_integration, "stop_retained_startup_hooks", _cleanup
    )

    async def _must_not_clone(*args, **kwargs):
        raise AssertionError("fresh reinstall must not clone while old code runs")

    monkeypatch.setattr(registry, "_clone_build_app", _must_not_clone)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert result["code"] == "startup_hook_still_running"
    assert result["retryable"] is True
    assert cleanup_calls == [("demoapp", True)]


@pytest.mark.asyncio
async def test_registry_fresh_reinstall_rechecks_retained_startup_before_replacement(
    monkeypatch, tmp_path
):
    """A fresh-looking reinstall must recheck ownership after clone and build."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "2.0.0"},
    )
    monkeypatch.setattr(registry, "get_app", lambda _name: None)

    from kiro_crew.apps import hooks_integration

    cleanup_calls: list[tuple[str, bool]] = []

    async def _cleanup(app_name: str, *, bounded: bool) -> bool:
        cleanup_calls.append((app_name, bounded))
        return len(cleanup_calls) == 1

    monkeypatch.setattr(
        hooks_integration, "stop_retained_startup_hooks", _cleanup
    )

    def _must_not_replace(*args, **kwargs):
        raise AssertionError("fresh reinstall must not replace files while old code runs")

    monkeypatch.setattr(registry, "install_app", _must_not_replace)
    monkeypatch.setattr(registry, "update_app", _must_not_replace)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert result["code"] == "startup_hook_still_running"
    assert result["retryable"] is True
    assert cleanup_calls == [("demoapp", True), ("demoapp", True)]


@pytest.mark.asyncio
async def test_registry_reinstall_rechecks_retained_startup_before_replacement(
    monkeypatch, tmp_path
):
    """A hook retained during clone/build must still block old-file replacement."""
    src = tmp_path / "app-sources" / "demoapp"
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "2.0.0"},
    )
    monkeypatch.setattr(registry, "get_app", lambda _name: {"name": "demoapp"})

    from kiro_crew.apps import hooks_integration

    cleanup_calls: list[tuple[str, bool]] = []

    async def _cleanup(app_name: str, *, bounded: bool) -> bool:
        cleanup_calls.append((app_name, bounded))
        # No task at admission time; one becomes retained while clone/build runs.
        return len(cleanup_calls) == 1

    monkeypatch.setattr(
        hooks_integration, "stop_retained_startup_hooks", _cleanup
    )

    def _must_not_update(*args, **kwargs):
        raise AssertionError("registry reinstall must not replace old files")

    monkeypatch.setattr(registry, "update_app", _must_not_update)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert result["code"] == "startup_hook_still_running"
    assert result["retryable"] is True
    assert cleanup_calls == [("demoapp", True), ("demoapp", True)]


@pytest.mark.asyncio
async def test_identity_gate_runs_before_the_build(monkeypatch, tmp_path):
    """A mismatched repo must be refused BEFORE _run_app_build executes — build
    ecosystems run repo-authored lifecycle scripts (npm preinstall, setup.py),
    so a post-build gate would let rejected code execute anyway."""
    src = tmp_path / "app-sources" / "demoapp"
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.json").write_text(json.dumps({"name": "evil-app"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)

    build_calls: list[object] = []

    async def _fake_run_build(build_dir, app_name, log_lines):
        build_calls.append(build_dir)
        return {"ok": True}

    monkeypatch.setattr(registry, "_run_app_build", _fake_run_build)

    result = await registry._clone_build_app(
        "https://example.com/demo.git", "demoapp", [], entry_repo="example/demo"
    )

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    assert build_calls == []  # the build never ran


@pytest.mark.asyncio
async def test_cloned_manifest_admission_is_revalidated_before_build(monkeypatch, tmp_path):
    """The repository can advance between the pre-clone prefetch and the clone:
    a signed preview resolving to an unsigned/banned manifest must be refused
    on the CLONED manifest, before any build command runs."""
    src = tmp_path / "app-sources" / "demoapp"
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)

    admission_calls: list[object] = []

    def _deny_cloned(name, *, manifest=None, action=""):
        admission_calls.append(manifest)
        return "signature required but manifest is unsigned"

    monkeypatch.setattr(registry, "app_admission_denied", _deny_cloned)

    build_calls: list[object] = []

    async def _fake_run_build(build_dir, app_name, log_lines):
        build_calls.append(build_dir)
        return {"ok": True}

    monkeypatch.setattr(registry, "_run_app_build", _fake_run_build)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert "admission" in result["error"]
    assert build_calls == []  # refused before the build
    assert len(admission_calls) == 1  # the cloned manifest was what got checked


@pytest.mark.asyncio
async def test_reused_checkout_pull_never_repoints_origin(monkeypatch, tmp_path):
    """The reuse path only runs after the origin-mismatch gate has verified the
    checkout's origin is byte-identical to the catalog URL (a mismatch is moved
    aside and re-cloned). It must therefore pull directly — never rewrite the
    origin remote — so the fetched code and the persisted provenance URL name
    the same repository by construction, not by mutation."""
    dest = tmp_path / "demoapp"
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)

    async def _fake_origin(path):
        return "https://example.com/new-home.git"

    monkeypatch.setattr(registry, "_clone_origin_url", _fake_origin)

    monkeypatch.setattr(registry, "_read_clone_branch", lambda clone_dir: "main")

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    err = await registry._git_clone_or_pull("https://example.com/new-home.git", "main", dest, [])

    assert err is None
    assert spawned[0][:2] == ["git", "pull"]
    assert not any(cmd[:3] == ["git", "remote", "set-url"] for cmd in spawned)


@pytest.mark.asyncio
async def test_clone_stream_never_emits_embedded_https_credentials(
    monkeypatch, tmp_path
):
    secret = "StreamingCloneSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    queue: asyncio.Queue[str] = asyncio.Queue()
    log_lines = registry.StreamingLogLines(queue)
    spawned: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry,
        "minimal_env",
        lambda **extra: {
            "BASE": "1",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/tmp/repository-controlled-hooks",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "/tmp/repository-controlled-fsmonitor",
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "!malicious-helper",
            "GIT_CONFIG_KEY_3": "filter.leak.process",
            "GIT_CONFIG_VALUE_3": "malicious-filter",
            **extra,
        },
    )

    class _Proc:
        def __init__(self, returncode=0, output=b""):
            self.returncode = returncode
            self.output = output

        pid = 4242

        async def communicate(self):
            return self.output, b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append((list(argv), kwargs))
        if "init" in argv:
            (tmp_path / "demo" / ".git").mkdir(parents=True)
        if "fetch" in argv:
            return _Proc(1, f"fatal: unable to access {raw_url}".encode())
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)

    result = await registry._git_clone_or_pull(
        public_url,
        "main",
        tmp_path / "demo",
        log_lines,
        credential_target=raw_url,
    )
    streamed = []
    while not queue.empty():
        streamed.append(queue.get_nowait())
    visible = "\n".join([*log_lines, *streamed, str(result)])

    assert secret not in visible
    assert raw_url not in visible
    assert public_url in visible
    assert "git transport output redacted (credentialed remote)" in visible
    # The network mapping may carry the raw credential in process memory, but
    # only fetch receives it; init, origin setup and checkout use the base env.
    assert spawned
    fetch_argv, spawn_kwargs = next(call for call in spawned if "fetch" in call[0])
    assert public_url in fetch_argv
    assert secret not in "\n".join(fetch_argv)
    transport_env = spawn_kwargs["env"]
    assert isinstance(transport_env, dict)
    pairs = _command_git_config(transport_env)
    assert pairs[:4] == [
        ("core.hooksPath", "/tmp/repository-controlled-hooks"),
        ("core.fsmonitor", "/tmp/repository-controlled-fsmonitor"),
        ("credential.helper", "!malicious-helper"),
        ("filter.leak.process", "malicious-filter"),
    ]
    _assert_credential_transport_hardening(transport_env, raw_url, public_url)


@pytest.mark.asyncio
async def test_credentialed_pull_appends_exec_neutralizers_after_inherited_config(
    monkeypatch, tmp_path
):
    secret = "PullTransportSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    dest = tmp_path / "demo"
    (dest / ".git").mkdir(parents=True)
    spawned: list[tuple[list[str], dict[str, object]]] = []
    log_lines: list[str] = []

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "_read_clone_branch", lambda clone_dir: "main")
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        registry,
        "minimal_env",
        lambda **extra: {
            "BASE": "1",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/tmp/repository-controlled-hooks",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "/tmp/repository-controlled-fsmonitor",
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "!malicious-helper",
            "GIT_CONFIG_KEY_3": "filter.leak.process",
            "GIT_CONFIG_VALUE_3": "malicious-filter",
            **extra,
        },
    )

    async def _fake_origin(path):
        return public_url

    monkeypatch.setattr(registry, "_clone_origin_url", _fake_origin)

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"Already up to date.", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append((list(argv), kwargs))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)

    result = await registry._git_clone_or_pull(
        public_url,
        "main",
        dest,
        log_lines,
        credential_target=raw_url,
    )

    assert result is None
    assert len(spawned) == 5
    assert not any(argv[:2] == ["git", "pull"] for argv, _ in spawned)
    fetch_argv, spawn_kwargs = next(call for call in spawned if "fetch" in call[0])
    assert public_url in fetch_argv
    assert "--no-auto-maintenance" in fetch_argv
    assert secret not in repr(fetch_argv)
    assert secret not in "\n".join(log_lines)
    transport_env = spawn_kwargs["env"]
    assert isinstance(transport_env, dict)
    assert _command_git_config(transport_env)[:4] == [
        ("core.hooksPath", "/tmp/repository-controlled-hooks"),
        ("core.fsmonitor", "/tmp/repository-controlled-fsmonitor"),
        ("credential.helper", "!malicious-helper"),
        ("filter.leak.process", "malicious-filter"),
    ]
    _assert_credential_transport_hardening(transport_env, raw_url, public_url)
    for argv, kwargs in spawned:
        if "fetch" not in argv:
            assert kwargs["env"] == {
                "BASE": "1",
                "GIT_CONFIG_COUNT": "4",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": "/tmp/repository-controlled-hooks",
                "GIT_CONFIG_KEY_1": "core.fsmonitor",
                "GIT_CONFIG_VALUE_1": "/tmp/repository-controlled-fsmonitor",
                "GIT_CONFIG_KEY_2": "credential.helper",
                "GIT_CONFIG_VALUE_2": "!malicious-helper",
                "GIT_CONFIG_KEY_3": "filter.leak.process",
                "GIT_CONFIG_VALUE_3": "malicious-filter",
            }


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="real Git hook inheritance and executable-bit semantics are POSIX-only",
)
def test_credential_transport_env_prevents_checkout_and_merge_hooks_from_seeing_secret(
    tmp_path,
):
    """A Git child carrying the one-shot credential must not run hooks.

    The control commands prove both hooks are executable and receive the raw
    command-scope rewrite. The same checkout/merge with the production env must
    leave no marker, pinning the behavior rather than only the env's shape.
    """

    secret = "HookInheritanceSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    hooks = tmp_path / "hooks"
    checkout_marker = tmp_path / "post-checkout-ran"
    merge_marker = tmp_path / "post-merge-ran"
    repo.mkdir()
    home.mkdir()
    hooks.mkdir()

    fixture_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Registry Test",
        "GIT_AUTHOR_EMAIL": "registry@example.invalid",
        "GIT_COMMITTER_NAME": "Registry Test",
        "GIT_COMMITTER_EMAIL": "registry@example.invalid",
    }
    commands: list[list[str]] = []

    def _git(*args: str, env: dict[str, str] | None = None) -> None:
        argv = ["git", *args]
        commands.append(argv)
        try:
            completed = subprocess.run(
                argv,
                cwd=repo,
                env=env or fixture_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")
        assert completed.returncode == 0, completed.stderr

    _git("init", "--quiet", "--template=")
    _git("checkout", "--quiet", "-b", "main")
    (repo / "payload.txt").write_text("base\n", encoding="utf-8")
    _git("add", "payload.txt")
    _git("commit", "--quiet", "--no-verify", "-m", "base")
    _git("checkout", "--quiet", "-b", "topic")
    (repo / "payload.txt").write_text("topic\n", encoding="utf-8")
    _git("commit", "--quiet", "--no-verify", "-am", "topic")
    _git("checkout", "--quiet", "main")

    # The vulnerable control env is the former shape: inherited hooksPath plus
    # the raw insteadOf mapping, without the fixed neutralizers.
    inherited = {
        **fixture_env,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(hooks),
    }
    protected = registry._git_transport_env(raw_url, public_url, inherited)
    protected_count = int(protected["GIT_CONFIG_COUNT"])
    vulnerable_count = protected_count - 4
    vulnerable = {
        key: value
        for key, value in protected.items()
        if not (key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"))
        or int(key.rsplit("_", 1)[1]) < vulnerable_count
    }
    vulnerable["GIT_CONFIG_COUNT"] = str(vulnerable_count)
    raw_mapping_index = vulnerable_count - 1

    for hook_name, marker in (
        ("post-checkout", checkout_marker),
        ("post-merge", merge_marker),
    ):
        hook = hooks / hook_name
        hook_body = "#!/bin/sh\n"
        hook_body += f'printf "%s" "$GIT_CONFIG_KEY_{raw_mapping_index}" > "{marker}"\n'
        hook.write_text(
            hook_body,
            encoding="utf-8",
        )
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    # Prove post-checkout is live and can read the embedded credential, then
    # repeat under the hardened environment.
    _git("checkout", "--quiet", "topic", env=vulnerable)
    assert secret in checkout_marker.read_text(encoding="utf-8")
    checkout_marker.unlink()
    _git("checkout", "--quiet", "main", env=protected)
    _git("checkout", "--quiet", "topic", env=protected)
    assert not checkout_marker.exists()
    _git("checkout", "--quiet", "main", env=protected)

    # The same control/protected pair for the hook that `git pull` runs after a
    # successful merge.
    _git("merge", "--quiet", "--ff-only", "topic", env=vulnerable)
    assert secret in merge_marker.read_text(encoding="utf-8")
    _git("reset", "--quiet", "--hard", "HEAD^", env=fixture_env)
    merge_marker.unlink()
    _git("merge", "--quiet", "--ff-only", "topic", env=protected)
    assert not merge_marker.exists()

    assert secret not in repr(commands)


def test_credential_transport_env_prevents_askpass_from_seeing_secret(tmp_path):
    """The protected transport disables a real inherited askpass command."""

    secret = "AskPassInheritanceSecret"
    raw_url = f"https://user:{secret}@example.invalid/o/demo.git"
    public_url = "https://example.invalid/o/demo.git"
    marker = tmp_path / "askpass-marker"
    askpass = tmp_path / "askpass"
    askpass.write_text(
        "#!/bin/sh\n"
        'printf "%s" "$GIT_CONFIG_KEY_1" > askpass-marker\n'
        'printf "supplied\\n"\n',
        encoding="utf-8",
    )
    askpass.chmod(askpass.stat().st_mode | stat.S_IXUSR)

    inherited = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.askPass",
        "GIT_CONFIG_VALUE_0": str(askpass),
    }
    protected = registry._git_transport_env(raw_url, public_url, inherited)
    protected_count = int(protected["GIT_CONFIG_COUNT"])
    vulnerable_count = protected_count - 4
    vulnerable = {
        key: value
        for key, value in protected.items()
        if not (key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"))
        or int(key.rsplit("_", 1)[1]) < vulnerable_count
    }
    vulnerable["GIT_CONFIG_COUNT"] = str(vulnerable_count)

    def _credential_fill(env):
        try:
            return subprocess.run(
                ["git", "credential", "fill"],
                cwd=tmp_path,
                env=env,
                input="protocol=https\nhost=example.invalid\nusername=user\n\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")

    control = _credential_fill(vulnerable)
    assert control.returncode == 0, control.stderr
    assert secret in marker.read_text(encoding="utf-8")
    marker.unlink()

    hardened = _credential_fill(protected)
    assert hardened.returncode != 0
    assert not marker.exists()


def test_checkout_filter_sees_secret_only_when_given_network_environment(tmp_path):
    """A real smudge filter proves why fetch and checkout need separate envs."""
    secret = "FilterInheritanceSecret"
    raw_url = f"https://user:{secret}@example.invalid/o/demo.git"
    public_url = "https://example.invalid/o/demo.git"
    repo = tmp_path / "repo"
    marker = tmp_path / "filter-marker"
    filter_script = tmp_path / "filter.py"
    repo.mkdir()
    filter_script.write_text(
        "import os, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "os.environ.get('GIT_CONFIG_KEY_1', ''), encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    filter_command = f'"{sys.executable}" "{filter_script}"'
    fixture_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Registry Test",
        "GIT_AUTHOR_EMAIL": "registry@example.invalid",
        "GIT_COMMITTER_NAME": "Registry Test",
        "GIT_COMMITTER_EMAIL": "registry@example.invalid",
    }

    def _git(*args, env):
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=repo,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")
        assert completed.returncode == 0, completed.stderr

    _git("init", "--quiet", "--template=", env=fixture_env)
    (repo / ".gitattributes").write_text("payload.txt filter=leak\n", encoding="utf-8")
    (repo / "payload.txt").write_text("payload\n", encoding="utf-8")
    _git("add", ".gitattributes", "payload.txt", env=fixture_env)
    _git("commit", "--quiet", "--no-verify", "-m", "base", env=fixture_env)

    checkout_env = {
        **fixture_env,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "filter.leak.smudge",
        "GIT_CONFIG_VALUE_0": filter_command,
    }
    network_env = registry._git_transport_env(raw_url, public_url, checkout_env)

    # Vulnerable combined clone/pull control: the fetched tree chooses the filter,
    # and the filter inherits the raw URL rewrite from the network subprocess.
    (repo / "payload.txt").unlink()
    _git("checkout", "--", "payload.txt", env=network_env)
    assert secret in marker.read_text(encoding="utf-8")

    # Production split: checkout still honours the configured filter, but receives
    # only the base environment, so there is no credential to inherit.
    marker.unlink()
    (repo / "payload.txt").unlink()
    _git("checkout", "--", "payload.txt", env=checkout_env)
    assert marker.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("raw_url", "reason"),
    [
        (
            "https://example.com/o/demo.git?repo=A&access_token=secret-a",
            "query or fragment",
        ),
        (
            "https://example.com/o/demo.git?repo=B&access_token=secret-b",
            "query or fragment",
        ),
        ("ssh://deploy@example.com/o/demo.git#private-ref", "query or fragment"),
        (
            "deploy:password@example.invalid:Owner/Repo.git",
            "ambiguous Git transport identity",
        ),
        (
            "ssh://deploy:password@example.invalid/Owner/Repo.git",
            "ambiguous Git transport identity",
        ),
        (
            "git+ssh://deploy:password@example.invalid/Owner/Repo.git",
            "ambiguous Git transport identity",
        ),
    ],
)
def test_git_transport_rejects_unsupported_identity_split(raw_url, reason):
    safe_url = registry._strip_git_target_userinfo(raw_url)

    with pytest.raises(ValueError, match=reason):
        registry._git_transport_env(raw_url, safe_url, {"BASE": "1"})


@pytest.mark.parametrize("scheme", ["ftp", "git+https", "custom"])
def test_git_transport_rejects_credentialed_remote_helper_schemes(scheme):
    raw_url = f"{scheme}://user:secret@example.invalid/o/demo.git"
    safe_url = f"{scheme}://example.invalid/o/demo.git"

    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        registry._git_transport_env(raw_url, safe_url, {"BASE": "1"})


@pytest.mark.asyncio
async def test_branch_fetch_rejects_credentialed_remote_helper_before_spawn(
    monkeypatch, tmp_path
):
    async def _must_not_spawn(*args, **kwargs):
        raise AssertionError("unsupported credential transport must fail before git")

    monkeypatch.setattr(registry, "create_subprocess_limited", _must_not_spawn)
    dest = tmp_path / "checkout"
    result = await registry._git_fetch_branch(
        "git+https://example.invalid/o/demo.git",
        "main",
        dest,
        [],
        credential_target="git+https://user:secret@example.invalid/o/demo.git",
        clone_env={"BASE": "1"},
        sandbox_mode="strict",
    )

    assert result is not None and result["ok"] is False
    assert "HTTP(S)" in result["error"]
    assert not dest.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "deploy:password@example.invalid:Owner/Repo.git",
        "ssh://deploy:password@example.invalid/Owner/Repo.git",
    ],
)
async def test_clone_build_rejects_ambiguous_transport_before_git_or_filesystem(
    monkeypatch, raw
):
    async def _must_not_clone(*args, **kwargs):
        raise AssertionError("ambiguous Git target must fail before git")

    monkeypatch.setattr(registry, "_git_clone_or_pull", _must_not_clone)
    result = await registry._clone_build_app(raw, "demoapp", [])

    assert result["ok"] is False
    assert "ambiguous Git transport" in result["error"]
    assert raw not in result["error"]


@pytest.mark.asyncio
async def test_pinned_fetch_isolates_credentials_to_network_transport(monkeypatch, tmp_path):
    """Only the fetch subprocess receives the command-scoped auth mapping.

    The generic sandbox boundary and the three local git operations see only
    the credential-free repository identity and base environment.
    """
    secret = "PinnedFetchSecret"
    raw_url = f"https://user:{secret}@example.com/o/demo.git"
    public_url = "https://example.com/o/demo.git"
    dest = tmp_path / "demo"
    commit = "a" * 40
    spawned: list[tuple[list[str], dict[str, object]]] = []
    wrapped: list[list[str]] = []

    def _fake_wrap(cmd, mode=""):
        wrapped.append(list(cmd))
        return cmd, None

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append((list(argv), kwargs))
        if "init" in argv:
            (dest / ".git").mkdir(parents=True)
        return _Proc()

    monkeypatch.setattr(registry, "wrap_argv", _fake_wrap)
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: commit)

    result = await registry._git_fetch_commit(
        public_url,
        commit,
        dest,
        [],
        credential_target=raw_url,
        clone_env={"BASE": "1"},
        sandbox_mode="strict",
    )

    assert result is None
    assert len(wrapped) == len(spawned) == 4
    assert all(secret not in "\n".join(argv) for argv in wrapped)
    assert all(secret not in "\n".join(argv) for argv, _ in spawned)
    assert any(argv[:4] == ["git", "remote", "add", "origin"] for argv in wrapped)
    assert public_url in "\n".join(part for argv in wrapped for part in argv)

    for argv, kwargs in spawned:
        process_env = kwargs["env"]
        assert isinstance(process_env, dict)
        if "fetch" in argv:
            _assert_credential_transport_hardening(process_env, raw_url, public_url)
        else:
            assert secret not in repr(process_env)


@pytest.mark.asyncio
async def test_branch_fetch_materializes_tracking_branch_with_real_git(monkeypatch, tmp_path):
    """The split fetch has real clone-equivalent branch/tracking semantics."""
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    dest = tmp_path / "checkout"
    work.mkdir()

    def _git(*args, cwd):
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("git is unavailable")
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    _git("init", "--quiet", "--template=", cwd=work)
    _git("checkout", "--quiet", "-b", "main", cwd=work)
    (work / "payload.txt").write_text("branch payload\n", encoding="utf-8")
    _git("add", "payload.txt", cwd=work)
    _git(
        "-c",
        "user.name=Registry Test",
        "-c",
        "user.email=registry@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
        cwd=work,
    )
    _git("clone", "--quiet", "--bare", str(work), str(remote), cwd=tmp_path)

    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    result = await registry._git_fetch_branch(
        str(remote),
        "main",
        dest,
        [],
        clone_env=registry.minimal_env(),
        sandbox_mode="strict",
    )

    assert result is None
    assert _git("branch", "--show-current", cwd=dest) == "main"
    assert _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=dest) == "origin/main"
    assert (dest / "payload.txt").read_text(encoding="utf-8") == "branch payload\n"


@pytest.mark.asyncio
async def test_failed_credentialed_update_restores_same_origin_checkout(monkeypatch, tmp_path):
    dest = tmp_path / "demo"
    (dest / ".git").mkdir(parents=True)
    (dest / "local-edit.txt").write_text("preserve me", encoding="utf-8")
    public_url = "https://example.com/o/demo.git"
    raw_url = "https://user:secret@example.com/o/demo.git"
    pending: list = []
    restorable: list = []

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)

    async def _origin(path):
        return public_url

    async def _failed_fetch(*args, **kwargs):
        assert not dest.exists(), "same-origin checkout must be isolated before network fetch"
        return {"ok": False, "error": "fetch failed"}

    monkeypatch.setattr(registry, "_clone_origin_url", _origin)
    monkeypatch.setattr(registry, "_git_fetch_branch", _failed_fetch)

    result = await registry._git_clone_or_pull(
        public_url,
        "main",
        dest,
        [],
        credential_target=raw_url,
        pending_cleanup=pending,
        restorable_stale=restorable,
    )

    assert result == {"ok": False, "error": "fetch failed"}
    assert (dest / "local-edit.txt").read_text(encoding="utf-8") == "preserve me"
    assert pending == []
    assert restorable == []


@pytest.mark.asyncio
async def test_double_cancel_waits_for_fetch_cleanup_before_restoring_checkout(
    monkeypatch, tmp_path
):
    """A background deleter must never outlive rollback to the same path."""
    dest = tmp_path / "demo"
    (dest / ".git").mkdir(parents=True)
    (dest / "local-edit.txt").write_text("preserve me", encoding="utf-8")
    public_url = "https://example.com/o/demo.git"
    raw_url = "https://user:secret@example.com/o/demo.git"
    fetch_started = asyncio.Event()
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    cleanup_done = threading.Event()
    cleanup_calls = 0

    async def _origin(_path):
        return public_url

    class _Proc:
        pid = 4242
        returncode = 0

        def __init__(self, argv):
            self.argv = argv

        async def communicate(self):
            if "init" in self.argv:
                (dest / ".git").mkdir(parents=True, exist_ok=True)
            if "fetch" in self.argv:
                fetch_started.set()
                await asyncio.Event().wait()
            return b"", b""

    async def _spawn(*argv, **_kwargs):
        return _Proc(argv)

    def _gated_rmtree(path):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            cleanup_started.set()
            assert cleanup_release.wait(30), "cleanup release was never signalled"
        shutil.rmtree(path, ignore_errors=True)
        if cleanup_calls == 1:
            cleanup_done.set()

    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda _url: True)
    monkeypatch.setattr(registry, "_clone_origin_url", _origin)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
    monkeypatch.setattr(registry, "_kill_process_group", AsyncMock())
    monkeypatch.setattr(platform_compat, "rmtree_force", _gated_rmtree)

    task = asyncio.create_task(
        registry._git_clone_or_pull(
            public_url,
            "main",
            dest,
            [],
            credential_target=raw_url,
        )
    )
    await asyncio.wait_for(fetch_started.wait(), timeout=5)
    task.cancel()
    assert await asyncio.to_thread(cleanup_started.wait, 5)

    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done(), "repeated cancellation escaped before cleanup settled"

    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleanup_done.is_set()
    assert (dest / "local-edit.txt").read_text(encoding="utf-8") == "preserve me"
    await asyncio.sleep(0.05)
    assert (dest / "local-edit.txt").is_file(), "late cleanup deleted restored checkout"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="onInstall scripts run via /bin/bash; the rewrite scenario is POSIX-only",
)
async def test_install_script_rewriting_manifest_is_refused(monkeypatch, tmp_path):
    """onInstall runs with write access to the checkout; if it rewrites
    app.json to a different identity, registration must be refused — the
    post-script re-read is what install_app would otherwise consume."""
    src = tmp_path / "app-sources" / "demoapp"
    script = "python3 -c \"import json;json.dump({'name':'evil-app'},open('app.json','w'))\""
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "setup": {"onInstall": script}},
    )

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()


@pytest.mark.asyncio
async def test_failed_pull_aborts_instead_of_installing_stale_code(monkeypatch, tmp_path):
    """A failed fast-forward pull must abort the operation: installing the
    checkout's stale contents while recording the catalog URL as provenance
    would persist a source the code was never fetched from."""
    dest = tmp_path / "demoapp"
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)

    async def _fake_origin(path):
        return "https://example.com/demo.git"

    monkeypatch.setattr(registry, "_clone_origin_url", _fake_origin)

    monkeypatch.setattr(registry, "_read_clone_branch", lambda clone_dir: "main")

    class _Proc:
        pid = 4242

        def __init__(self, rc):
            self.returncode = rc

        async def communicate(self):
            return b"", b""

    rcs = iter([1])  # the pull (first and only spawn) fails

    async def _fake_spawn(*argv, **kwargs):
        return _Proc(next(rcs))

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    err = await registry._git_clone_or_pull(
        "https://example.com/demo.git", "main", dest, []
    )

    assert err is not None and err["ok"] is False
    assert "stale" in err["error"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="onInstall scripts run via /bin/bash; the swap scenario is POSIX-only",
)
async def test_provenance_signer_uses_post_script_manifest(monkeypatch, tmp_path):
    """onInstall can replace app.json with a differently signed, still-valid
    manifest; provenance must record the FINAL manifest's signer."""
    src = tmp_path / "app-sources" / "demoapp"
    script = (
        "python3 -c \"import json;json.dump("
        "{'name':'demoapp','version':'2.0.0'},open('app.json','w'))\""
    )
    _identity_harness(
        monkeypatch,
        src,
        cloned_manifest={"name": "demoapp", "version": "1.0.0", "setup": {"onInstall": script}},
    )
    commit_reads: list[int] = []

    def _commit_by_read_order(root):
        # First read (if any) would be pre-script; the fix resolves it ONCE,
        # post-script — emulate a script advancing the checkout by returning a
        # different SHA per read and asserting the LAST one is persisted.
        commit_reads.append(len(commit_reads))
        return ("c" if len(commit_reads) == 1 else "d") * 40

    monkeypatch.setattr(registry, "_resolved_clone_commit", _commit_by_read_order)

    def _signer_by_version(manifest):
        return f"signer-of-{getattr(manifest, 'version', '?')}"

    monkeypatch.setattr(registry, "verified_signer", _signer_by_version)

    provenance: dict[str, object] = {}
    monkeypatch.setattr(
        registry,
        "set_app_provenance",
        lambda name, **kwargs: provenance.update(kwargs, name=name),
    )
    monkeypatch.setattr(registry, "get_app", lambda n: None)
    monkeypatch.setattr(
        registry,
        "install_app",
        lambda path, **kwargs: MagicMock(
            ok=True, name="demoapp", message="done", error=""
        ),
    )

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is True
    # The script swapped in v2.0.0; the signer must be v2's, not v1's.
    assert provenance["signer"] == "signer-of-2.0.0"
    # And the commit is resolved exactly once, post-script — never a stale
    # pre-script read.
    assert len(commit_reads) == 1
    assert provenance["commit"] == "c" * 40


@pytest.mark.asyncio
async def test_admission_rejection_deletes_fresh_clone(monkeypatch, tmp_path):
    """A fresh clone rejected by the cloned-manifest admission gate must leave
    no residue — a leftover would be preferred by the prefetch and poison
    every subsequent attempt."""
    src = tmp_path / "app-sources" / "demoapp"
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(
        registry, "app_admission_denied", lambda *a, **k: "unsigned under policy"
    )

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert not src.exists()


@pytest.mark.asyncio
async def test_admission_rejection_rolls_back_preexisting_checkout(monkeypatch, tmp_path):
    """A pre-existing checkout whose pull advanced to a policy-rejected commit
    is rolled back to its pre-pull commit, keeping retries viable."""
    src = tmp_path / "app-sources" / "demoapp"
    (src / ".git").mkdir(parents=True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "b" * 40)

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        (dest / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(
        registry, "app_admission_denied", lambda *a, **k: "unsigned under policy"
    )

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert (src / ".git").is_dir()  # workspace preserved
    assert spawned and spawned[0][:4] == ["git", "reset", "--keep", "b" * 40]


@pytest.mark.asyncio
async def test_postbuild_admission_rejection_deletes_fresh_clone(monkeypatch, tmp_path):
    """The POST-BUILD admission denial must clean the checkout exactly like the
    cloned-admission gate: a fresh clone left at the rejected commit would be
    preferred by the prefetch and poison every retry."""
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": False,
            "_pre_pull_commit": "",
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    calls = {"n": 0}

    def _deny_postbuild(*a, **k):
        calls["n"] += 1
        # 1st call = prefetch admission (pass); 2nd = post-build (deny).
        return "unsigned under policy" if calls["n"] >= 2 else None

    monkeypatch.setattr(registry, "app_admission_denied", _deny_postbuild)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "admission policy" in result["error"]
    assert not src.exists()  # fresh clone removed — retry starts clean


@pytest.mark.asyncio
async def test_postscript_admission_rejection_rolls_back_preexisting_checkout(
    monkeypatch, tmp_path
):
    """The POST-SCRIPT admission denial must roll a pre-existing checkout back
    to its pre-pull commit — onInstall already ran with write access, so the
    checkout is otherwise left poisoned at the rejected state."""
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "app.json").write_text(
        json.dumps({"name": "demoapp", "setup": {"onInstall": "true"}}), encoding="utf-8"
    )
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": True,
            "_pre_pull_commit": "b" * 40,
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    calls = {"n": 0}

    def _deny_postscript(*a, **k):
        calls["n"] += 1
        # 1st = prefetch (pass); 2nd = post-build (pass); 3rd = post-script (deny).
        return "unsigned under policy" if calls["n"] >= 3 else None

    monkeypatch.setattr(registry, "app_admission_denied", _deny_postscript)

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    reaped: list[int] = []

    async def _fake_tree_kill(pid, sig):
        reaped.append(pid)

    def _fake_killpg(pgid, sig):
        reaped.append(pgid)

    monkeypatch.setattr(
        registry.platform_compat, "kill_process_tree_async", _fake_tree_kill
    )
    monkeypatch.setattr(registry.os, "killpg", _fake_killpg, raising=False)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "admission policy" in result["error"]
    assert (src / ".git").is_dir() or src.exists()  # workspace preserved, not deleted
    assert any(cmd[:4] == ["git", "reset", "--keep", "b" * 40] for cmd in spawned)
    # Surviving onInstall descendants are reaped BEFORE the final gates
    # re-read the manifest (closes the detached-child rewrite TOCTOU).
    assert 4242 in reaped
    # The manifest file is restored from HEAD as well: a script rewriting
    # app.json is a working-tree edit the reset alone cannot undo.
    assert any(
        cmd[:4] == ["git", "--literal-pathspecs", "checkout", "--"] for cmd in spawned
    )


@pytest.mark.asyncio
async def test_moveaside_reclone_retained_not_restored_on_rejection(monkeypatch, tmp_path):
    """When the origin-mismatch gate moves an old checkout aside and
    fresh-clones, a rejection must delete the fresh re-clone (never preserve it
    or reset it toward the moved-aside repository's commit) and must NOT
    restore the moved-aside previous checkout: an origin-mismatch move-aside is
    a DIFFERENT repository, so handing it back to the slot would give a later
    retry the very tree this gate already refused. It stays RETAINED as a
    `.stale-*` sibling (recoverable by hand, swept on a retention timer), which
    is why only a same-origin/branch-drift move-aside is ever restored."""
    src = tmp_path / "app-sources" / "demoapp"
    (src / ".git").mkdir(parents=True)  # OLD checkout pre-exists (origin A)
    (src / "old-work.txt").write_text("precious", encoding="utf-8")
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "a" * 40)

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        # Simulate the origin-mismatch move-aside + fresh re-clone. Only
        # `pending_cleanup` is populated, never `restorable_stale` — an
        # origin-mismatch move is never restorable.
        moved = dest.with_name("demoapp.stale-deadbeef")
        dest.rename(moved)
        cleanup = kwargs.get("pending_cleanup")
        if cleanup is not None:
            cleanup.append(moved)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.json").write_text(json.dumps({"name": "demoapp"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(
        registry, "app_admission_denied", lambda *a, **k: "unsigned under policy"
    )

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    # No rollback is attempted toward the moved-aside repo's commit ...
    assert not any(cmd[:3] == ["git", "reset", "--keep"] for cmd in spawned)
    # ... the rejected re-clone is gone from the active slot ...
    assert not src.exists()
    # ... and the ORIGIN-mismatched previous checkout is retained, not
    # restored into the slot the gate just refused it for.
    stale = src.with_name("demoapp.stale-deadbeef")
    assert stale.exists()
    assert (stale / "old-work.txt").read_text(encoding="utf-8") == "precious"


@pytest.mark.asyncio
async def test_rejection_restores_users_pre_update_manifest_bytes(monkeypatch, tmp_path):
    """A rejection on a pre-existing checkout must restore app.json to its
    exact PRE-UPDATE working-tree bytes — including the user's uncommitted
    local edits — not to HEAD's version, which would silently discard them."""
    user_manifest = b'{"name": "demoapp", "_user_note": "my local tweak"}'
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)
    (src / "app.json").write_bytes(b'{"name": "demoapp"}')  # post-build (poisoned) state
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": True,
            "_pre_pull_commit": "b" * 40,
            "_pre_update_manifest": user_manifest,
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    calls = {"n": 0}

    def _deny_postbuild(*a, **k):
        calls["n"] += 1
        return "unsigned under policy" if calls["n"] >= 2 else None

    monkeypatch.setattr(registry, "app_admission_denied", _deny_postbuild)

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    # The manifest holds the user's exact pre-update bytes again ...
    assert (src / "app.json").read_bytes() == user_manifest
    # ... restored from the snapshot, not via a HEAD checkout.
    assert not any(cmd[:2] == ["git", "--literal-pathspecs"] for cmd in spawned)
    assert any(cmd[:4] == ["git", "reset", "--keep", "b" * 40] for cmd in spawned)


@pytest.mark.asyncio
async def test_identity_refusal_rolls_back_preexisting_checkout(monkeypatch, tmp_path):
    """An IDENTITY refusal on a pre-existing checkout (pull brought a
    self-renaming manifest) must roll the workspace back like the admission
    gates do — preserved but left at the renamed manifest, the prefetch would
    re-reject every retry before a fixed remote could be pulled."""
    src = tmp_path / "app-sources" / "demoapp"
    (src / ".git").mkdir(parents=True)
    (src / "keep.txt").write_text("user state", encoding="utf-8")
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda root: "b" * 40)

    async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
        (dest / "app.json").write_text(json.dumps({"name": "renamed-app"}), encoding="utf-8")
        return None

    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)

    spawned: list[list[str]] = []

    class _Proc:
        returncode = 0
        pid = 4242

        async def communicate(self):
            return b"", b""

    async def _fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)

    result = await registry._clone_build_app("https://example.com/demo.git", "demoapp", [])

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    # Workspace preserved ...
    assert (src / ".git").is_dir()
    assert (src / "keep.txt").read_text(encoding="utf-8") == "user state"
    # ... but un-poisoned: rolled back to the pre-pull commit AND the manifest
    # restored from HEAD.
    assert any(cmd[:4] == ["git", "reset", "--keep", "b" * 40] for cmd in spawned)
    assert any(
        cmd[:4] == ["git", "--literal-pathspecs", "checkout", "--"] for cmd in spawned
    )


@pytest.mark.asyncio
async def test_build_deleting_manifest_is_refused_with_cleanup(monkeypatch, tmp_path):
    """A build step that DELETES app.json must go through the identity refusal
    (fail-closed) and its checkout cleanup — not an early return that leaves a
    fresh checkout poisoned in the app-sources slot."""
    src = tmp_path / "app-sources" / "demoapp"
    src.mkdir(parents=True)  # fresh clone; build "deleted" app.json — none written
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(
        registry,
        "get_registry_app",
        lambda n: {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"},
    )
    monkeypatch.setattr(
        registry,
        "_fetch_app_manifest",
        AsyncMock(return_value={"name": "demoapp", "version": "1.0.0"}),
    )
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)

    async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": False,
            "_pre_pull_commit": "",
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

    result = await registry.install_from_registry("demoapp")

    assert result["ok"] is False
    assert "refusing" in result["error"].lower()
    assert not src.exists()  # fresh checkout removed — no poisoned residue


def test_entry_git_url_tolerates_non_string_values():
    """An external index can carry an object-valued gitUrl; the resolver must
    degrade to "no URL" instead of crashing every caller with AttributeError."""
    assert registry._entry_git_url({"gitUrl": {"evil": True}}) == ""
    assert registry._entry_git_url({"gitUrl": ["x"], "repo": None}) == ""
    assert registry._entry_git_url({"repo": 42}) == ""
    assert registry._entry_git_url({"gitUrl": " https://ok.example/r.git "}) == "https://ok.example/r.git"


class TestMinimalEnvHonorsWindowsCaseInsensitivity:
    """Windows env names are case-INSENSITIVE and `os.environ` upper-cases keys.

    So `os.environ.items()` yields `SYSTEMROOT`, never the `SystemRoot` spelling
    Microsoft documents and that the allowlist writes. A literal membership test
    therefore dropped exactly the variables the list carries for Windows — silently
    at the boundary, fatally in the child: a Windows process without `SystemRoot`
    cannot resolve side-by-side assemblies and dies before `main()`.
    """

    def test_upper_cased_windows_keys_are_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            registry.os,
            "environ",
            {"SYSTEMROOT": r"C:\Windows", "USERPROFILE": r"C:\Users\me", "TEMP": r"C:\Temp"},
        )
        env = registry.minimal_env()
        assert env["SYSTEMROOT"] == r"C:\Windows"
        assert env["USERPROFILE"] == r"C:\Users\me"
        assert env["TEMP"] == r"C:\Temp"

    def test_folding_did_not_admit_secrets(self, monkeypatch) -> None:
        """The fold widens CASE, never the key set."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            registry.os,
            "environ",
            {"SYSTEMROOT": r"C:\Windows", "GITHUB_TOKEN": "ghp_x", "AWS_SECRET_ACCESS_KEY": "s"},
        )
        env = registry.minimal_env()
        assert "GITHUB_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env

    def test_posix_matching_stays_exact(self, monkeypatch) -> None:
        """`PATH` and `Path` are DIFFERENT variables on POSIX.

        Folding there would let a lookalike through, so the fold is Windows-only.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(registry.os, "environ", {"PATH": "/usr/bin", "Path": "/sneaky"})
        env = registry.minimal_env()
        assert env["PATH"] == "/usr/bin"
        assert "Path" not in env


class TestApplyTrustFields:
    """``_apply_trust_fields`` is the API trust boundary of
    ``GET /api/apps/registry`` (issue #580): ``provenance``/``verified`` are
    computed server-side where the ``_registry`` tag is authoritative, and
    ``featured`` is stripped from external rows. Every branch below mirrors a
    spoof that used to be blocked only by scattered client-side checks.
    """

    def test_external_entry_is_never_verified_despite_spoofed_fields(self):
        """An external index publishing author/origin/featured spoofs gains
        nothing: the row is external because the server tagged it."""
        entry = {
            "name": "evil-app",
            "_registry": "evil-registry",
            "author": "KiroCrew",       # brand-ok: author-spoof fixture
            "origin": "builtin",        # origin spoof
            "featured": True,           # spotlight self-flag
        }
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "external"
        assert out["verified"] is False
        assert "featured" not in out

    def test_external_entry_cannot_pre_seed_trust_fields(self):
        """Index-published ``provenance``/``verified`` values are OVERWRITTEN,
        not merely defaulted — otherwise an index could ship them directly."""
        entry = {
            "name": "evil-app",
            "_registry": "evil-registry",
            "provenance": "official",
            "verified": True,
        }
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "external"
        assert out["verified"] is False

    def test_stargazers_count_is_stripped_from_external_rows(self):
        """A trust cue only the publisher may mint — the ``featured``
        precedent: an external index self-reporting ``★ 50K`` on a hostile
        app would render identically to a signed-catalog count, so the field
        is dropped entirely from external rows regardless of validity."""
        entry = {"name": "ext-app", "_registry": "labs", "stargazersCount": 1234}
        (out,) = registry._apply_trust_fields([entry])
        assert "stargazersCount" not in out

    def test_stargazers_count_survives_on_non_external_rows(self):
        """Catalog/seed rows (no ``_registry`` marker) keep a sanitized count;
        zero is a real count, distinct from the absent field."""
        entry = {"name": "cat-app", "stargazersCount": 1234}
        (out,) = registry._apply_trust_fields([entry])
        assert out["stargazersCount"] == 1234
        entry_zero = {"name": "cat-app-2", "stargazersCount": 0}
        (out_zero,) = registry._apply_trust_fields([entry_zero])
        assert out_zero["stargazersCount"] == 0

    @pytest.mark.parametrize(
        "bad",
        [-1, "1234", True, False, 3.5, None, [12], {"n": 12}, 9_007_199_254_740_992],
        ids=["negative", "string", "true", "false", "float", "none", "list", "dict", "over-js-max"],
    )
    def test_stargazers_count_non_int_values_are_dropped(self, bad):
        """The shape check runs on the boundary EVERY row crosses
        (``_resolve_manifest`` passes the row through unchanged on a failed
        manifest fetch, so the allowlist projection is not a guaranteed
        exit) — a malformed count is dropped, never coerced. The upper bound
        is the JS safe-integer range: Python accepts a 309-digit int that
        JavaScript renders as hundreds of digits."""
        entry = {"name": "seed-app", "stargazersCount": bad}
        (out,) = registry._apply_trust_fields([entry])
        assert "stargazersCount" not in out

    def test_stargazers_count_at_the_js_safe_integer_bound_survives(self):
        entry = {"name": "cat-app", "stargazersCount": 9_007_199_254_740_991}
        (out,) = registry._apply_trust_fields([entry])
        assert out["stargazersCount"] == 9_007_199_254_740_991

    def test_stargazers_count_sanitized_on_non_external_rows_too(self):
        """Sanitization applies to every row, not just the external branch."""
        entry = {"name": "seed-app", "stargazersCount": "9999"}
        (out,) = registry._apply_trust_fields([entry])
        assert "stargazersCount" not in out

    def test_builtin_row_without_stargazers_count_is_unaffected(self):
        entry = {"name": "demo", "origin": "builtin"}
        (out,) = registry._apply_trust_fields([entry])
        assert "stargazersCount" not in out
        assert out["provenance"] == "builtin"
        assert out["verified"] is True

    def test_clone_target_is_server_overwritten_and_prefers_git_url(self):
        """The modal must show what install will clone, not the legacy alias.

        External rows legitimately carry both fields with different values;
        ``_entry_git_url`` makes ``gitUrl`` authoritative. An index-supplied
        ``trustRepository`` is ignored rather than becoming consent authority.
        """
        entry = {
            "name": "external-app",
            "_registry": "labs",
            "repo": "https://example.test/display/alias",
            "gitUrl": "HTTPS://Clone.Example.test/Owner/App.git/",
            "trustRepository": "https://evil.example/spoof",
        }
        (out,) = registry._apply_trust_fields([entry])
        assert out["trustRepository"] == "https://clone.example.test/Owner/App"

    def test_clone_target_normalization_strips_userinfo_preserves_port_and_path(self):
        target = "HTTPS://User:SeCrEt@EXAMPLE.COM:08443/Owner/Repo.git/"

        assert registry._normalize_git_target(target) == (
            "https://example.com:08443/Owner/Repo"
        )

    def test_metadata_normalization_redacts_colon_bearing_ssh_userinfo(self):
        target = "SSH://Git:T%2FAB@[2001:DB8::A]:2222/Owner/Repo"

        assert registry._normalize_git_target(target) == (
            "ssh://Git@[2001:db8::a]:2222/Owner/Repo"
        )

    def test_clone_target_normalization_drops_query_and_fragment_secrets(self):
        assert registry._normalize_git_target(
            "HTTPS://EXAMPLE.COM?Ref=Case#Frag"
        ) == "https://example.com"

    def test_clone_target_userinfo_is_not_repository_identity(self):
        assert registry._same_git_target(
            "https://User:Secret@EXAMPLE.COM/o/r",
            "https://user:secret@example.com/o/r",
        )

    def test_clone_target_scp_username_is_preserved_without_folding_path(self):
        assert registry._normalize_git_target("Git@EXAMPLE.COM:Owner/Repo") == (
            "Git@EXAMPLE.COM:Owner/Repo"
        )
        assert not registry._same_git_target(
            "Git@EXAMPLE.COM:Owner/Repo", "EXAMPLE.COM:owner/Repo"
        )

    @pytest.mark.parametrize(
        "target,expected",
        [
            (
                "https://user:token@example.test/Owner/Repo.git",
                "https://example.test/Owner/Repo.git",
            ),
            (
                "https://example.test/Owner/Repo.git?access_token=secret#private",
                "https://example.test/Owner/Repo.git",
            ),
            (
                "ssh://deploy@example.test/Owner/Repo.git",
                "ssh://deploy@example.test/Owner/Repo.git",
            ),
            (
                "ssh://deploy:password@example.test/Owner/Repo.git",
                "ssh://deploy@example.test/Owner/Repo.git",
            ),
            (
                "git+ssh://deploy:password@example.test/Owner/Repo.git",
                "git+ssh://deploy@example.test/Owner/Repo.git",
            ),
            (
                "deploy@example.test:Owner/Repo.git",
                "deploy@example.test:Owner/Repo.git",
            ),
            (
                "deploy:password@example.test:Owner/Repo.git",
                "deploy@example.test:Owner/Repo.git",
            ),
            (
                "deploy:password@[unterminated:Owner/Repo.git",
                "deploy@[unterminated:Owner/Repo.git",
            ),
        ],
    )
    def test_clone_target_secret_stripping_preserves_ssh_routing(
        self, target: str, expected: str
    ):
        assert registry._strip_git_target_userinfo(target) == expected

    def test_colon_bearing_ssh_userinfo_is_never_a_repository_identity(self):
        assert not registry._same_git_target(
            "ssh://deploy:old@example.test/Owner/Repo.git",
            "ssh://deploy:new@example.test/Owner/Repo.git",
        )
        assert not registry._same_git_target(
            "ssh://deploy:old@example.test/Owner/Repo.git",
            "ssh://release:old@example.test/Owner/Repo.git",
        )
        assert not registry._same_git_target(
            "ssh://deploy:old@example.test/Owner/Repo.git",
            "ssh://deploy:old@example.test/Owner/Repo.git",
        )

    @pytest.mark.parametrize("suffix", ["?repo=A", "#private-ref"])
    def test_unsupported_suffix_is_never_a_repository_identity(self, suffix):
        ambiguous = f"https://example.test/Owner/Repo.git{suffix}"
        safe = "https://example.test/Owner/Repo.git"

        assert not registry._same_git_target(ambiguous, safe)
        assert not registry._same_git_target(ambiguous, ambiguous)

    def test_ambiguous_scp_prefix_is_never_a_repository_identity(self):
        raw = "deploy:password@example.invalid:Owner/Repo.git"
        sanitized = "deploy@example.invalid:Owner/Repo.git"

        assert registry._git_target_has_ambiguous_scp_prefix(raw)
        assert not registry._same_git_target(raw, sanitized)
        assert not registry._same_git_target(raw, raw)

    def test_ambiguous_ssh_userinfo_is_never_a_repository_identity(self):
        raw = "ssh://deploy:password@example.invalid/Owner/Repo.git"
        sanitized = "ssh://deploy@example.invalid/Owner/Repo.git"

        assert registry._git_target_has_ambiguous_ssh_userinfo(raw)
        assert not registry._same_git_target(raw, sanitized)
        assert not registry._same_git_target(raw, raw)

    def test_clone_target_scp_parser_handles_long_unterminated_authority_linearly(self):
        """An attacker-sized invalid SCP target stays local/unmodified.

        This shape made the former nested/alternating fullmatch backtrack over
        the entire authority.  The deterministic parser performs only bounded
        ``find``/scan passes, so increasing the input cannot cause regex DoS.
        """
        target = "user@[" + ("a" * 200_000) + ":Owner/Repo"

        assert registry._strip_git_target_userinfo(target) == target
        assert registry._normalize_git_target(target) == target

    def test_clone_target_ports_are_not_silently_equivalent(self):
        assert not registry._same_git_target(
            "https://example.com:8443/o/r",
            "https://example.com:8444/o/r",
        )
        assert not registry._same_git_target(
            "https://example.com:443/o/r",
            "https://example.com/o/r",
        )

    def test_clone_target_ipv6_host_case_is_cosmetic_but_path_case_is_not(self):
        assert registry._same_git_target(
            "ssh://git@[2001:DB8::A]:2222/Owner/Repo",
            "SSH://git@[2001:db8::a]:2222/Owner/Repo",
        )
        assert not registry._same_git_target(
            "ssh://git@[2001:DB8::A]:2222/Owner/Repo",
            "ssh://git@[2001:db8::a]:2222/owner/Repo",
        )

    @pytest.mark.parametrize(
        "target",
        [
            r"C:\Work\Owner\Repo",
            "/Tmp/Owner/Repo",
        ],
    )
    def test_clone_target_non_uri_forms_are_not_guessed(self, target: str):
        assert registry._normalize_git_target(target) == target

    def test_clone_target_malformed_unbracketed_ipv6_is_fail_conservative(self):
        assert registry._normalize_git_target(
            "SSH://2001:DB8::1/Owner/Repo"
        ) == "ssh://2001:DB8::1/Owner/Repo"

    def test_clone_target_spoof_is_removed_when_no_repository_resolves(self):
        entry = {
            "name": "local-app",
            "trustRepository": "https://evil.example/spoof",
        }
        (out,) = registry._apply_trust_fields([entry])
        assert "trustRepository" not in out

    def test_storefront_clone_coordinates_never_expose_userinfo(self):
        secret = "SuperSecret"
        entry = {
            "name": "credentialed-app",
            "gitUrl": f"HTTPS://User:{secret}@Clone.Example.test/Owner/App.git",
            "repo": f"https://Alias:{secret}@display.example.test/Owner/App",
        }

        (out,) = registry._apply_trust_fields([entry])

        assert out["trustRepository"] == "https://clone.example.test/Owner/App"
        assert out["gitUrl"] == "HTTPS://Clone.Example.test/Owner/App.git"
        assert out["repo"] == "https://display.example.test/Owner/App"
        assert secret not in str(out)

    @pytest.mark.parametrize("suffix", ["?repo=A&access_token=secret", "#private"])
    def test_storefront_never_mints_proof_for_unsupported_clone_suffix(self, suffix):
        entry = {
            "name": "ambiguous-app",
            "gitUrl": f"https://clone.example.test/Owner/App.git{suffix}",
        }

        (out,) = registry._apply_trust_fields([entry])

        assert "trustRepository" not in out
        assert "secret" not in str(out)

    @pytest.mark.parametrize(
        ("raw", "sanitized"),
        [
            (
                "deploy:password@example.invalid:Owner/App.git",
                "deploy@example.invalid:Owner/App.git",
            ),
            (
                "ssh://deploy:password@example.invalid/Owner/App.git",
                "ssh://deploy@example.invalid/Owner/App.git",
            ),
        ],
    )
    def test_storefront_never_mints_proof_for_ambiguous_git_target(
        self, raw, sanitized
    ):
        entry = {"name": "ambiguous-app", "gitUrl": raw}

        (out,) = registry._apply_trust_fields([entry])

        assert "trustRepository" not in out
        assert out["gitUrl"] == sanitized
        assert raw not in str(out)

    def test_legacy_query_selected_repository_cannot_resolve_as_trust_proof(self):
        installed = {
            "name": "legacy-app",
            "source": "registry:legacy-app",
            "sourceUrl": "https://clone.example.test/Owner/App.git?repo=A",
        }
        assert registry.resolve_installed_trust_repository(installed) == (False, "")

        installed.pop("sourceUrl")
        assert registry.resolve_installed_trust_repository(
            installed,
            registry_entry={
                "gitUrl": "https://clone.example.test/Owner/App.git?repo=B"
            },
        ) == (False, "")

    @pytest.mark.parametrize(
        "raw",
        [
            "deploy:password@example.invalid:Owner/App.git",
            "ssh://deploy:password@example.invalid/Owner/App.git",
        ],
    )
    def test_legacy_ambiguous_repository_cannot_resolve_as_trust_proof(self, raw):
        installed = {
            "name": "legacy-app",
            "source": "registry:legacy-app",
            "sourceUrl": raw,
        }
        assert registry.resolve_installed_trust_repository(installed) == (False, "")

        installed.pop("sourceUrl")
        assert registry.resolve_installed_trust_repository(
            installed, registry_entry={"gitUrl": raw}
        ) == (False, "")

    def test_legacy_installed_row_uses_current_authoritative_clone_target(self):
        entry = {
            "name": "legacy-app",
            "repo": "https://example.test/display/alias",
            "gitUrl": "https://clone.example.test/Owner/current-app.git",
        }
        installed = {
            "legacy-app": {
                "name": "legacy-app",
                "source": "registry:legacy-app",
            }
        }

        bindings = registry._trust_repository_bindings([entry], installed)
        [out] = registry._apply_trust_fields(
            [entry], trust_repositories=bindings
        )

        assert out["trustRepository"] == (
            "https://clone.example.test/Owner/current-app"
        )

    def test_local_installed_row_does_not_inherit_same_named_registry_target(self):
        entry = {
            "name": "local-app",
            "gitUrl": "https://clone.example.test/Owner/registry-app.git",
        }
        installed = {
            "local-app": {
                "name": "local-app",
                "source": "C:/operator/local-app",
                "origin": "local",
            }
        }

        bindings = registry._trust_repository_bindings([entry], installed)
        [out] = registry._apply_trust_fields(
            [entry], trust_repositories=bindings
        )

        assert "trustRepository" not in out

    def test_core_kirocrew_index_author_is_verified(self):
        """``verified`` derives from the INDEX-declared author snapshot
        (``_index_author``, taken by ``list_registry`` pre-merge)."""
        entry = {"name": "good-app", "_index_author": "KiroCrew"}  # brand-ok: author-spoof fixture
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "official"
        assert out["verified"] is True

    def test_manifest_author_alone_never_mints_verified(self):
        """A third-party core repo publishing ``"author": "kirocrew"`` in its
        app.json gains nothing: the merged ``author`` display field is not
        consulted, only the pre-merge index snapshot is."""
        entry = {"name": "sneaky", "author": "KiroCrew"}  # merged, no snapshot  # brand-ok: author-spoof fixture
        (out,) = registry._apply_trust_fields([entry])
        assert out["verified"] is False
        entry = {"name": "sneaky2", "author": "KiroCrew", "_index_author": "third-party"}  # brand-ok: author-spoof fixture
        (out,) = registry._apply_trust_fields([entry])
        assert out["verified"] is False

    def test_core_third_party_author_is_not_verified_and_keeps_featured(self):
        entry = {"name": "community-app", "_index_author": "someone", "featured": 2}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "official"
        assert out["verified"] is False
        assert out["featured"] == 2  # curator flag preserved for core entries

    def test_builtin_origin_is_verified_builtin(self):
        entry = {"name": "builtin-app", "origin": "builtin", "author": "x"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "builtin"
        assert out["verified"] is True

    def test_non_string_index_author_does_not_crash_and_is_not_verified(self):
        """External registries are user-supplied JSON; a mistyped author must
        degrade to unverified, not raise."""
        entry = {"name": "weird", "_index_author": 42}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "official"
        assert out["verified"] is False

    def test_bundled_seed_row_is_official_not_a_separate_value(self):
        """The bundled ``app-registry.json`` is the OFFLINE SEED of the list we
        publish, not a different kind of app, so it carries the same provenance
        a signed remote catalog will. Giving the seed its own value would put a
        weaker integrity guarantee — it rides on the install artifact and cannot
        be revoked before the next release — behind a label the client cannot
        tell apart from the stronger one. Provenance names WHOSE list an app is
        on; how the list arrived is a separate axis."""
        entry = {"name": "launchdarkly", "repo": "https://example.com/org/app"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "official"

    def test_index_author_snapshot_never_leaks_into_payload(self):
        entry = {"name": "x", "_index_author": "KiroCrew"}  # brand-ok: author-spoof fixture
        (out,) = registry._apply_trust_fields([entry])
        assert "_index_author" not in out

    def test_two_word_org_spelling_is_verified(self):
        """The product name is two words, and both the bundled catalog and the
        official published catalog state the org that way. A single-token-only
        comparison silently un-verified every first-party app whose index row
        spelled the org correctly."""
        entry = {"name": "spec-builder", "_index_author": "Kiro Crew"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["verified"] is True

    @pytest.mark.parametrize(
        "spelling",
        [
            "Ｋｉｒｏ　Ｃｒｅｗ",  # fullwidth, ideographic space
            "kiro\u200bcrew",  # zero-width space
            "Kiro\u00adCrew",  # soft hyphen
            "  kiro   crew  ",  # padded, doubled inner space
            "KIROCREW",
        ],
    )
    def test_first_party_spelling_variants_still_verify(self, spelling):
        """A row we ship or sign may legitimately name us in a non-ASCII form.
        Folding (NFKC + drop category-Cf + collapse whitespace) keeps the mark
        instead of dropping it on a spelling difference a human cannot see."""
        entry = {"name": "app", "_index_author": spelling}
        (out,) = registry._apply_trust_fields([entry])
        assert out["verified"] is True

    def test_folding_does_not_grant_the_mark_to_an_external_row(self):
        """The fold widens the match, so pin the short-circuit that keeps it
        harmless: a tagged row is unverified BEFORE the author is consulted."""
        entry = {"name": "app", "_registry": "labs", "_index_author": "Kiro Crew"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["provenance"] == "external"
        assert out["verified"] is False

    def test_near_miss_author_is_not_verified(self):
        """Folding must not blur a DIFFERENT name into ours."""
        for name in ("kiro crews", "kiro-crew", "kirocrew labs", "crew kiro"):
            entry = {"name": "app", "_index_author": name}
            (out,) = registry._apply_trust_fields([entry])
            assert out["verified"] is False, name

    def test_registry_tag_is_kept_in_payload(self):
        """``_registry`` stays in the row — the external-source label text and
        older clients still need it. The change ADDS fields only."""
        entry = {"name": "ext", "_registry": "labs"}
        (out,) = registry._apply_trust_fields([entry])
        assert out["_registry"] == "labs"

    @pytest.mark.asyncio
    async def test_list_registry_stamps_trust_fields(self, monkeypatch):
        """End-to-end: every row returned by ``list_registry`` carries the
        server-computed fields; external spoofs and a manifest-published
        ``author: "kirocrew"`` are all neutralized."""
        core = {"name": "core-app", "author": "KiroCrew", "featured": 1}  # brand-ok: author-spoof fixture
        # Third-party core entry whose REPO manifest claims the first-party
        # author (index declares none) — must not mint the badge.
        sneaky = {"name": "sneaky-app"}
        # Index entry trying to pre-seed the internal snapshot key directly.
        preseed = {"name": "preseed-app", "_index_author": "KiroCrew"}  # brand-ok: author-spoof fixture
        ext = {
            "name": "ext-app",
            "_registry": "labs",
            "author": "KiroCrew",  # brand-ok: author-spoof fixture
            "origin": "builtin",
            "featured": True,
        }
        monkeypatch.setattr(
            registry, "_load_registry_file", lambda: [core, sneaky, preseed]
        )

        async def _fake_external():
            return [ext]

        async def _fake_resolve(entry):
            # Simulate the app.json merge overwriting the display author.
            if entry["name"] == "sneaky-app":
                return {**entry, "author": "KiroCrew"}  # brand-ok: author-spoof fixture
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        rows = {r["name"]: r for r in await registry.list_registry()}
        assert rows["core-app"]["provenance"] == "official"
        assert rows["core-app"]["verified"] is True
        assert rows["core-app"]["featured"] == 1
        # Manifest-published author does not mint the badge.
        assert rows["sneaky-app"]["verified"] is False
        # Pre-seeded snapshot key is overwritten from the entry's own author
        # (absent here) before the manifest merge.
        assert rows["preseed-app"]["verified"] is False
        assert rows["ext-app"]["provenance"] == "external"
        assert rows["ext-app"]["verified"] is False
        assert "featured" not in rows["ext-app"]
        # The internal snapshot key never leaks into the API payload.
        assert all("_index_author" not in r for r in rows.values())


# ---------------------------------------------------------------------------
# External registries must surface on the ONLINE catalog path.
#
# Regression: handle_registry prefers list_catalog_apps when the published
# catalog is reachable and only falls back to list_registry (the sole path that
# merged external registries) when the catalog is empty. So a configured
# external app was silently dropped from the store the moment the catalog came
# online. list_catalog_apps now appends external-registry rows itself.
# ---------------------------------------------------------------------------
_PINNED_SHA = "a" * 40


def _pinned_catalog_entry(name: str) -> dict[str, Any]:
    """A catalog entry `official_catalog.inventory` accepts as installable.

    The real `inventory` runs over this, so the coordinates have to satisfy its
    validation (https clone URL, full-length lowercase-hex pin) rather than being
    waved through by a stub.
    """
    return {
        "name": name,
        "source": {
            "type": "git",
            "url": f"https://github.com/org/{name}",
            "ref": _PINNED_SHA,
        },
    }


class TestCatalogAppsIncludesExternalRegistries:
    @pytest.mark.asyncio
    async def test_external_registry_app_appears_when_catalog_is_online(self, monkeypatch):
        """With a NON-EMPTY catalog (so the catalog path is taken), a configured
        external app still shows up — tagged external, not-installed — and a
        same-named catalog row wins the dedup."""
        # Catalog is reachable and non-empty: this forces the list_catalog_apps
        # path rather than the offline list_registry fallback.
        catalog_rows = [
            {"name": "catalog-app", "displayName": "Catalog App"},
            # Collision: the catalog also lists a name an external registry uses.
            {"name": "shared-app", "displayName": "Official Shared App"},
        ]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        # Seed only matters for the git-row installable filter; keep it empty.
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        external_rows = [
            {"name": "labs-app", "repo": "x", "_registry": "labs"},
            # Same name as a catalog row — the catalog row must win.
            {"name": "shared-app", "repo": "y", "_registry": "labs"},
        ]

        async def _fake_external():
            return external_rows

        async def _fake_resolve(entry):
            # Manifests already present in the index fixture; return as-is.
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}

        # The external-only app is present, tagged external, and not installed.
        assert "labs-app" in rows
        assert rows["labs-app"]["_registry"] == "labs"
        assert rows["labs-app"]["provenance"] == "external"
        assert rows["labs-app"]["verified"] is False
        assert rows["labs-app"]["installed"] is False
        # The collision resolves to the catalog row (official), not the external one.
        assert rows["shared-app"]["displayName"] == "Official Shared App"
        assert rows["shared-app"]["provenance"] != "external"
        # The plain catalog app is untouched.
        assert "catalog-app" in rows
        # The internal snapshot key never leaks into the API payload.
        assert all("_index_author" not in r for r in rows.values())

    @pytest.mark.asyncio
    async def test_detect_installed_only_external_app_reads_installed_on_catalog_path(
        self, monkeypatch
    ):
        """Install-status PARITY with the offline path: an external app known ONLY
        via its detectInstalled probe (absent from installed_map) must read
        installed=True on the ONLINE catalog path too, because that path now runs
        the same probe and passes the resulting `detected` into enrichment."""
        monkeypatch.setattr(
            registry.official_catalog,
            "list_catalog_rows",
            lambda: [{"name": "catalog-app", "displayName": "Catalog App"}],
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        async def _fake_external():
            return [
                {
                    "name": "detect-app",
                    "repo": "z",
                    "_registry": "labs",
                    "detectInstalled": "true",
                }
            ]

        async def _fake_resolve(entry):
            return entry

        # Stand in for the real subprocess probe: report installed the same way
        # _detect_installed_probe would for a returncode-0 command.
        async def _fake_probe(entries, installed_map):
            return {e["name"] for e in entries if e.get("detectInstalled")}

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)
        monkeypatch.setattr(registry, "_detect_installed_probe", _fake_probe)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert rows["detect-app"]["installed"] is True

    @pytest.mark.asyncio
    async def test_external_row_cannot_shadow_filtered_catalog_git_name(self, monkeypatch):
        """GPT BLOCK regression: a catalog `git` row dropped by the installability
        filter must still RESERVE its name, so an external row with the same name
        is deduped away and can never become the row install-by-name resolves."""
        catalog_rows = [
            {"name": "keep-app", "displayName": "Keep App"},
            # git source, and NOT in the seed installable set below -> filtered out.
            {"name": "filtered-git", "source": {"type": "git"}},
        ]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        # The catalog pins nothing either, so the row stays filtered -- and the
        # listing never reaches for a real fetch.
        monkeypatch.setattr(
            registry.official_catalog, "fetch_inventory_entries", lambda: []
        )

        async def _fake_external():
            # External registry tries to claim the filtered-out catalog name.
            return [{"name": "filtered-git", "repo": "evil", "_registry": "labs"}]

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        # The catalog git row was filtered out AND the external row was reserved
        # away, so the name is absent entirely -- crucially it never appears as an
        # EXTERNAL row pointing at the "evil" repo.
        assert rows.get("filtered-git", {}).get("provenance") != "external"
        assert "filtered-git" not in rows

    # -----------------------------------------------------------------------
    # Regression: the storefront intersected every catalog `git` row with the
    # BUNDLED SEED, which ships only at release cadence. `inventory` was added so
    # the catalog itself could supply validated pinned coordinates, and the install
    # path honours them (`inventory_for_install`) -- but this listing was written a
    # day earlier and still asked the seed. The two resolvers disagreed: install
    # accepted a catalog-only app while the store hid it, so a freshly published
    # app was undiscoverable until a release shipped a new seed.
    # -----------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_a_catalog_pinned_git_row_is_listed_without_a_seed_entry(self, monkeypatch):
        """A `git` row the catalog PINS is listed even though the seed omits it."""
        catalog_rows = [
            {"name": "keep-app", "displayName": "Keep App"},
            {"name": "pinned-app", "source": {"type": "git"}},
        ]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        # The seed names NOTHING -- the catalog alone has to carry this row.
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        monkeypatch.setattr(
            registry.official_catalog,
            "fetch_inventory_entries",
            lambda: [_pinned_catalog_entry("pinned-app")],
        )

        async def _fake_external():
            return []

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert "pinned-app" in rows, "a catalog-pinned git row must reach the store"
        # It is an official row, and still unverified -- listing it does not mint
        # the first-party badge from a document trusted only as far as TLS.
        assert rows["pinned-app"]["provenance"] != "external"
        assert rows["pinned-app"]["verified"] is False
        assert rows["pinned-app"]["trustRepository"] == (
            "https://github.com/org/pinned-app"
        )
        assert "keep-app" in rows

    @pytest.mark.asyncio
    async def test_a_name_only_the_local_cache_claims_is_not_listed(self, monkeypatch):
        """The unlock is authorised by the FRESH document, never by the cache.

        `list_catalog_rows` reads the cache under the data home, which is
        agent-writable. A planted row there must not BECOME a listed row: it would
        render with official provenance and dedupe the real same-named external row
        out of the listing, so a consent prompt would describe an official app while
        the name grant it produces installs the external one.
        """
        monkeypatch.setattr(
            registry.official_catalog,
            "list_catalog_rows",
            lambda: [{"name": "planted-app", "source": {"type": "git"}}],
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        # The fetched document pins a DIFFERENT app, so it never authorises
        # "planted-app" -- only the cache claims that name.
        monkeypatch.setattr(
            registry.official_catalog,
            "fetch_inventory_entries",
            lambda: [_pinned_catalog_entry("honest-app")],
        )

        async def _fake_external():
            return []

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert "planted-app" not in rows

    @pytest.mark.asyncio
    async def test_an_unreachable_catalog_degrades_to_the_seed(self, monkeypatch):
        """A listing must never fail because the catalog cannot be reached.

        Without the fresh document there is nothing authorising the catalog-only
        row, so it stays filtered -- the listing this path produced before the
        catalog could supply coordinates -- and the rest of the store still renders.
        """
        catalog_rows = [
            {"name": "keep-app", "displayName": "Keep App"},
            {"name": "pinned-app", "source": {"type": "git"}},
        ]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        monkeypatch.setattr(registry, "_load_registry_file", lambda: [])
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        def _unavailable():
            raise registry.official_catalog.CatalogUnavailable("cdn is down")

        monkeypatch.setattr(
            registry.official_catalog, "fetch_inventory_entries", _unavailable
        )

        async def _fake_external():
            return []

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert "pinned-app" not in rows
        assert "keep-app" in rows, "the rest of the store still renders"

    @pytest.mark.asyncio
    async def test_no_fresh_fetch_when_the_seed_already_covers_every_git_row(
        self, monkeypatch
    ):
        """The storefront is the hot path, so the fetch is paid only when it can
        change the answer. With every `git` row already seeded there is nothing to
        unlock, and the listing must stay at one cached read."""
        catalog_rows = [{"name": "seeded-app", "source": {"type": "git"}}]
        monkeypatch.setattr(
            registry.official_catalog, "list_catalog_rows", lambda: catalog_rows
        )
        monkeypatch.setattr(
            registry,
            "_load_registry_file",
            lambda: [
                {
                    "name": "seeded-app",
                    "repo": "https://example.test/display/alias",
                    "gitUrl": "https://clone.example.test/owner/seeded-app.git",
                }
            ],
        )
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])

        calls: list[int] = []

        def _counting():
            calls.append(1)
            return []

        monkeypatch.setattr(
            registry.official_catalog, "fetch_inventory_entries", _counting
        )

        async def _fake_external():
            return []

        async def _fake_resolve(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _fake_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _fake_resolve)

        rows = {r["name"]: r for r in await registry.list_catalog_apps()}
        assert calls == [], "the seed already answers, so no fetch may be paid"
        assert "seeded-app" in rows
        assert rows["seeded-app"]["trustRepository"] == (
            "https://clone.example.test/owner/seeded-app"
        )


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

    async def _fake_clone(git_url, branch, pkg_dir, log_lines, **kwargs):
        sub = pkg_dir / "apps" / "my-tool"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "pyproject.toml").write_text("[project]\n", "utf-8")
        # The identity gate reads app.json from the declared subdirectory and
        # fails closed on a mismatch — the cloned repo must declare the name.
        (sub / "app.json").write_text(json.dumps({"name": "my-tool"}), "utf-8")
        return None

    monkeypatch.setattr(registry, "_run_app_build", _fake_build)
    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_source_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    await registry._clone_build_app_locked(
        "https://example.invalid/r.git",
        "my-tool",
        [],
        subdirectory="apps/my-tool",
        pending_cleanup=[],
    )

    assert captured, "the build must be attempted"
    assert (
        captured[0].name == "my-tool" and captured[0].parent.name == "apps"
    ), f"build ran in {captured[0]} — expected the declared subdirectory"


@pytest.mark.asyncio
async def test_a_traversing_subdirectory_does_not_choose_the_build_dir(tmp_path, monkeypatch):
    """``subdirectory`` is untrusted index content, so it must not escape the clone.

    The identity gate joins ``subdirectory`` under the clone root with a
    containment check and FAILS CLOSED on an escaping value — no build command
    may run in a directory chosen by a traversing path.
    """
    captured: list = []

    async def _fake_build(build_dir, app_name, log_lines):
        captured.append(build_dir)
        return {"ok": True}

    async def _fake_clone(git_url, branch, pkg_dir, log_lines, **kwargs):
        pkg_dir.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(registry, "_run_app_build", _fake_build)
    monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
    monkeypatch.setattr(registry, "app_source_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    result = await registry._clone_build_app_locked(
        "https://example.invalid/r.git",
        "evil",
        [],
        subdirectory="../../etc",
        pending_cleanup=[],
    )

    assert result["ok"] is False
    assert "unsafe subdirectory" in result["error"]
    assert captured == [], f"build ran despite a traversing subdirectory: {captured}"


class TestMergeManifestProjectsRegistryKeys:
    """``_merge_manifest`` starts from an explicit projection of the index row.

    An index row is untrusted content, so a key an index invents must not ride
    into the API payload just because the merge started from a copy of the row.
    """

    MANIFEST = {"name": "demo-app", "displayName": "Demo App", "version": "1.2.3"}

    def test_unknown_index_key_does_not_reach_the_row(self):
        entry = {
            "name": "demo-app",
            "repo": "DemoRepo",
            "surpriseKey": "whatever an index felt like publishing",
            "__proto__": {"polluted": True},
        }
        out = registry._merge_manifest(entry, self.MANIFEST)
        assert "surpriseKey" not in out
        assert "__proto__" not in out

    def test_index_cannot_publish_trust_or_install_state(self):
        """These are stamped server-side after the merge; an index value for
        them must not survive to be read before that happens."""
        entry = {
            "name": "demo-app",
            "repo": "DemoRepo",
            "provenance": "builtin",
            "verified": True,
            "installed": True,
            "enabled": True,
            "origin": "builtin",
            "lifecycle": "locked",
        }
        out = registry._merge_manifest(entry, self.MANIFEST)
        for key in ("provenance", "verified", "installed", "enabled", "origin", "lifecycle"):
            assert key not in out, key

    def test_index_cannot_override_manifest_display_copy(self):
        """Display fields come from the fetched app.json, so an index row that
        publishes its own must not win — nor survive alongside."""
        entry = {
            "name": "demo-app",
            "repo": "DemoRepo",
            "displayName": "Index Said This",
            "description": "index copy",
        }
        out = registry._merge_manifest(entry, self.MANIFEST)
        assert out["displayName"] == "Demo App"
        assert "description" not in out  # manifest carried none, so neither does the row

    @pytest.mark.parametrize(
        "key,value",
        [
            ("gitUrl", "https://example.com/org/app.git"),
            ("repo", "DemoRepo"),
            ("branch", "release"),
            ("subdirectory", "apps/demo"),
            ("resources", "app"),
            ("detectInstalled", "which demo"),
            ("managed", True),
            ("featured", 2),
            ("stargazersCount", 42),
            ("_registry", "labs"),
        ],
    )
    def test_registry_owned_keys_survive(self, key, value):
        """Each of these has a reader — the clone path, the install path, the
        spotlight, or the trust stamp. Dropping one breaks that reader."""
        entry = {"name": "demo-app", key: value}
        out = registry._merge_manifest(entry, self.MANIFEST)
        assert out[key] == value

    def test_index_author_snapshot_survives_the_merge(self):
        """``_apply_trust_fields`` runs AFTER the merge and consumes this key to
        decide the verified mark, so the projection has to carry it through."""
        entry = {"name": "demo-app", "_index_author": "Kiro Crew"}
        out = registry._merge_manifest(entry, self.MANIFEST)
        assert out["_index_author"] == "Kiro Crew"

    def test_dark_icon_path_becomes_a_blob_url(self):
        """A raster icon cannot repaint from theme tokens, so an app may ship a
        dark variant; it routes through the same proxy as the light one."""
        entry = {"name": "demo-app", "repo": "DemoRepo"}
        manifest = {**self.MANIFEST, "iconPath": "a/i.png", "iconPathDark": "a/i-dark.png"}
        out = registry._merge_manifest(entry, manifest)
        assert out["iconUrl"] == "/api/apps/blob?repo=DemoRepo&path=a/i.png"
        assert out["iconUrlDark"] == "/api/apps/blob?repo=DemoRepo&path=a/i-dark.png"

    def test_dark_icon_is_omitted_when_absent(self):
        """Absence must not publish an empty string: the client treats a falsy
        dark variant as "fall back to the light one", and an empty key would
        also widen the payload for every app that ships one icon."""
        entry = {"name": "demo-app", "repo": "DemoRepo"}
        out = registry._merge_manifest(entry, {**self.MANIFEST, "iconPath": "a/i.png"})
        assert "iconUrlDark" not in out

    def test_manifest_declared_icon_url_is_never_copied(self):
        """An index-fetched manifest is untrusted content. Honouring an absolute
        ``iconUrl`` from it would let a third party point the store's <img> at
        any host; only repo-relative paths rewritten through our proxy are used."""
        entry = {"name": "demo-app", "repo": "DemoRepo"}
        manifest = {
            **self.MANIFEST,
            "iconUrl": "https://evil.example/track.png",
            "iconUrlDark": "https://evil.example/track-dark.png",
        }
        out = registry._merge_manifest(entry, manifest)
        assert "iconUrl" not in out
        assert "iconUrlDark" not in out


class TestCatalogFailureNeverBreaksTheStore:
    """`list_registry` runs inside `GET /api/apps/registry` with no try/except
    above it, so anything escaping the catalog step is a 500 for the whole store.

    The catalog is an ENHANCEMENT to a listing that is already complete without
    it, which is what makes containment at this seam correct rather than merely
    defensive: the fallback is not a degraded guess, it is exactly what the store
    rendered before the catalog existed. Review found three separate escape
    routes inside the module, each narrower than this seam -- these tests pin the
    seam so a fourth one cannot reach a user.
    """

    async def _rows(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_load_registry_file", lambda: [{"name": "seed-app"}]
        )

        async def _no_external():
            return []

        async def _passthrough(entry):
            return entry

        monkeypatch.setattr(registry, "_load_external_registries", _no_external)
        monkeypatch.setattr(registry, "_resolve_manifest", _passthrough)
        monkeypatch.setattr(registry, "list_installed_apps", lambda: [])
        return {r["name"]: r for r in await registry.list_registry()}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("unexpected"),
            TypeError("a field was not the type we assumed"),
            KeyError("name"),
            AttributeError("None has no attribute get"),
            ValueError("bad value"),
        ],
        ids=lambda e: type(e).__name__,
    )
    async def test_a_raising_loader_still_returns_the_seed(self, exc, monkeypatch):
        def boom():
            raise exc

        monkeypatch.setattr(registry.official_catalog, "fetch_inventory_entries", boom)
        rows = await self._rows(monkeypatch)
        assert "seed-app" in rows, "the seed listing must survive a catalog failure"

    @pytest.mark.asyncio
    async def test_a_raising_annotate_still_returns_the_seed(self, monkeypatch):
        """The overlay is the half that touches untrusted field types, so it is
        the half most likely to raise on a document we did not anticipate."""
        monkeypatch.setattr(
            registry.official_catalog,
            "fetch_inventory_entries",
            lambda: [{"name": "seed-app"}],
        )

        def boom(rows, entries):
            raise TypeError("hostile field type")

        monkeypatch.setattr(registry.official_catalog, "annotate", boom)
        rows = await self._rows(monkeypatch)
        assert "seed-app" in rows

    @pytest.mark.asyncio
    async def test_the_failure_is_logged_rather_than_swallowed(
        self, monkeypatch, caplog
    ):
        """A broad catch is only acceptable because it is loud: without the
        traceback this would hide our own bugs instead of a bad document.

        Patched at `fetch_inventory_entries` because that is the source the listing
        now uses; the cache-fed loader is no longer on this path at all.
        """

        def boom():
            raise RuntimeError("kaboom")

        monkeypatch.setattr(
            registry.official_catalog, "fetch_inventory_entries", boom
        )
        with caplog.at_level("WARNING", logger=registry.logger.name):
            await self._rows(monkeypatch)
        assert any("catalog" in r.message for r in caplog.records)
        assert any(r.exc_info for r in caplog.records), "expected a traceback"


# ---------------------------------------------------------------------------
# A failed FRESH clone must actually remove the partial checkout on Windows.
#
# git writes `.git/objects/pack/*.{pack,idx,rev}` read-only. On Windows that is
# FILE_ATTRIBUTE_READONLY, so `shutil.rmtree(..., ignore_errors=True)` cannot
# unlink them and silently reports success over a tree that is still on disk.
# The update path already copes with a surviving tree -- its `finally` moves the
# leftover aside so the restore rename cannot collide -- but the fresh-install
# path had no such guard, so the leftover became permanent: the next install
# sees `dest/.git` with a matching origin, takes the fast-forward branch, and
# `git pull` in a never-finished clone fails on every retry.
#
# POSIX cannot express this: the read-only bit there does not govern unlink (the
# parent directory's write permission does), so `rmtree` succeeds either way and
# the test would be green before the fix. Hence a platform gate rather than a
# simulated failure -- the real file attribute is the entire mechanism.
# ---------------------------------------------------------------------------


def _partial_clone(dest):
    """What a `git clone` killed partway through leaves behind at *dest*."""
    pack = dest / ".git" / "objects" / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    (dest / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://example.com/demo.git\n', encoding="utf-8"
    )
    blob = pack / "pack-0123456789abcdef0123456789abcdef01234567.pack"
    blob.write_bytes(b"PACK")
    os.chmod(blob, stat.S_IREAD)
    return blob


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_POSIX,
    reason="the read-only attribute only blocks unlink on Windows",
)
async def test_manifest_and_index_temp_roots_remove_read_only_git_children(monkeypatch, tmp_path):
    """Both throwaway clone consumers must remove Windows read-only pack files."""
    roots = [tmp_path / "manifest-tmp", tmp_path / "registry-tmp"]
    returned_roots = iter(roots)
    raw = "https://user:secret@example.com/org/apps.git"

    def _mkdtemp(**_kwargs):
        root = next(returned_roots)
        root.mkdir(parents=True)
        return str(root)

    async def _fetch(_git_url, _branch, dest, _log_lines, **_kwargs):
        _partial_clone(dest)
        (dest / "app.json").write_text(json.dumps({"name": "private-app"}), encoding="utf-8")
        (dest / "app-registry.json").write_text(
            json.dumps([{"name": "private-app", "repo": raw}]),
            encoding="utf-8",
        )
        return None

    monkeypatch.setattr("tempfile.mkdtemp", _mkdtemp)
    monkeypatch.setattr(registry, "_git_fetch_branch", _fetch)
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda _url: True)
    monkeypatch.setattr(registry, "_sel_fn", None)

    manifest = await registry._fetch_app_manifest(
        raw,
        "main",
        git_url=raw,
        owner_designated=True,
    )
    index = await registry._fetch_external_registry_index(raw, "main")

    assert manifest == {"name": "private-app"}
    assert index is not None and index[0]["name"] == "private-app"
    assert all(not root.exists() for root in roots)


def _fresh_clone_harness(monkeypatch, dest, *, mode):
    """Patch registry so a fresh clone into *dest* fails in *mode*."""
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "_kill_process_group", AsyncMock())
    monkeypatch.setattr(registry, "_CLONE_TIMEOUT", 0.05)

    class _Proc:
        returncode = 1 if mode == "exit" else 0
        pid = 4242

        async def communicate(self):
            if mode == "timeout":
                await asyncio.sleep(30)
            if mode == "cancel":
                raise asyncio.CancelledError()
            return b"fatal: early EOF", b""

    async def _fake_spawn(*argv, **kwargs):
        # git created the destination and wrote pack files before it died.
        _partial_clone(dest)
        return _Proc()

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_POSIX,
    reason="the read-only attribute only blocks unlink on Windows",
)
@pytest.mark.parametrize("mode", ["exit", "timeout", "cancel"])
async def test_failed_fresh_clone_removes_read_only_partial_checkout(
    monkeypatch, tmp_path, mode
):
    """Every fresh-clone failure exit must leave no destination behind."""
    dest = tmp_path / "app-sources" / "demoapp"
    _fresh_clone_harness(monkeypatch, dest, mode=mode)

    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await registry._git_clone_or_pull(
                "https://example.com/demo.git", "main", dest, []
            )
    else:
        err = await registry._git_clone_or_pull(
            "https://example.com/demo.git", "main", dest, []
        )
        assert err is not None and err["ok"] is False

    assert not dest.exists(), (
        "the partial checkout survived: the next install would find its .git, "
        "take the fast-forward branch and fail on every retry"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    platform_compat.IS_POSIX,
    reason="the read-only attribute only blocks unlink on Windows",
)
async def test_failed_pinned_fetch_removes_read_only_partial_checkout(
    monkeypatch, tmp_path
):
    """The pinned path materialises its own destination with `git init`; a failed
    fetch must discard it as completely as the clone path does."""
    dest = tmp_path / "app-sources" / "pinnedapp"
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (cmd, None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "_kill_process_group", AsyncMock())

    class _Proc:
        pid = 4242

        def __init__(self, rc):
            self.returncode = rc

        async def communicate(self):
            return b"fatal: could not read from remote repository", b""

    async def _fake_spawn(*argv, **kwargs):
        if "init" in argv:
            _partial_clone(dest)  # git init made it; the fetch left pack files
            return _Proc(0)
        if "fetch" in argv:
            return _Proc(1)
        return _Proc(0)

    monkeypatch.setattr(registry, "create_subprocess_limited", _fake_spawn)

    err = await registry._git_fetch_commit(
        "https://example.com/demo.git",
        "a" * 40,
        dest,
        [],
        clone_env={},
        sandbox_mode="strict",
    )

    assert err is not None and err["ok"] is False
    assert not dest.exists(), "the pinned path left an undeletable checkout behind"
