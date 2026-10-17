"""Regression tests for subprocess-timeout remediation in apps/registry_pipeline/.

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
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture

from kiro_crew import platform_compat
from kiro_crew.apps import registry

# A portable long-lived child: sleeps well past any test timeout without
# relying on POSIX-only binaries (``sleep``/``bash`` are absent on native
# Windows, where they would fail collection with FileNotFoundError).
_SLEEP_SCRIPT = "import time; time.sleep(60)"


# A portable child that ignores SIGTERM so the group kill must escalate to
# SIGKILL to stop it. SIGTERM-ignore + SIGKILL escalation is POSIX signal
# semantics, so tests using this are guarded with skipif(not IS_POSIX).
#
# The self-exit deadline is load-bearing, not decoration: this child is spawned
# ``start_new_session``, so it leads its own group and a sweep of the pytest
# worker's group never reaches it, and it is SIGTERM-immune by construction, so
# the usual polite shutdown cannot end it either. Its ONLY reaper is the
# escalation under test — so if that escalation regresses (precisely what these
# tests exist to catch), or the worker is SIGKILLed by ``--timeout``/
# ``--max-worker-restart`` before the reap, where no ``finally`` would run, an
# unbounded ``while True`` loop would leave an immortal process pegged to the
# host until a reboot or a manual hunt. Bounding it matches the three sibling
# SIG_IGN children in this suite and does not soften any assertion: SIGTERM is
# ``SIG_IGN``, so it is never delivered to Python and cannot cut the sleep short
# via EINTR — the child is still alive and still immune across the grace period,
# and SIGKILL still lands at the same instant.
_SIGTERM_IGNORE_SCRIPT = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)"
)


class _TimeoutProc:
    """Fake subprocess whose ``communicate()`` times out.

    Lets us exercise the timeout branch instantly (no real long-running
    process) while recording whether the branch killed and reaped the child.
    """

    def __init__(self) -> None:
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on the fake process, moved verbatim; tracked for a de-flake pass
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


# --------------------------------------------------------------------------
# Shared helper: _communicate_with_timeout (mechanism behind bugs 1, 2a, 2b)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_communicate_with_timeout_kills_and_reaps_real_subprocess():
    """A hung child (its own session leader) must be group-killed AND reaped."""
    # flake-ok: pre-existing real sleeper child whose only reaper is the code under test; it exits on its own after 60 s, moved verbatim
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
    # never a bare wait() that a full pipe could hang ...
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
    monkeypatch.setattr(registry, "_enrich_with_install_status", lambda e, m, d: e)

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
# The off-network switch also covers the listing's per-row manifest clone
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_manifest_does_not_clone_under_the_skip_switch(monkeypatch, tmp_path):
    """A seed row with no cached manifest renders its minimal info, no clone.

    ``KIROCREW_SKIP_APP_STORE_FETCH=1`` is what the E2E harness gateway runs
    with; the seed fallback otherwise pays a shallow clone per row, bounded by
    the clone timeout, on a rig that promises to stay offline.
    """
    from kiro_crew.apps import official_catalog

    monkeypatch.setenv(official_catalog.SKIP_FETCH_ENV, "1")
    monkeypatch.setattr(registry, "_read_manifest_cache", lambda entry: None)

    async def _must_not_clone(*args, **kwargs):
        raise AssertionError("the listing reached the clone under the skip switch")

    monkeypatch.setattr(registry, "_fetch_app_manifest", _must_not_clone)
    entry = {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"}

    assert await registry._resolve_manifest(dict(entry)) == entry


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
    # flake-ok: pre-existing SIGTERM-immune child whose only reaper is the escalation under test; it exits at its own deadline, moved verbatim
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
    hang the app-build timeout path forever."""
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
                # flake-ok: the stub's own grace-wait hang, which the patched _KILL_GRACE_PERIOD cuts short; moved verbatim
                # nosemgrep: kirocrew.test-sleep-as-barrier -- the stub's own grace-wait hang, which _KILL_GRACE_PERIOD cuts short; not a barrier
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


class _ExitProc:
    """Fake subprocess that exits at once with a fixed return code."""

    def __init__(self, code: int) -> None:
        self.pid = None  # skips the post-exit straggler reap
        self.returncode: int | None = None
        self._code = code

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = self._code
        return b"secret-output", b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_proc", "expected"),
    [
        (_TimeoutProc, ("timed_out", " exit=-9")),
        (lambda: _ExitProc(3), ("failed", " exit=3")),
        (lambda: _ExitProc(0), ("completed", " exit=0")),
    ],
)
async def test_install_script_emits_terminal_sel_event(monkeypatch, tmp_path, make_proc, expected):
    """Every onInstall exit records a terminal SEL event after `started`."""
    entry = {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"}
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args, **kwargs):
        return {}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    audit = MagicMock()
    monkeypatch.setattr(registry, "sel", lambda: audit)
    (tmp_path / "app.json").write_text(
        json.dumps({"name": "demoapp", "setup": {"onInstall": "true"}}), encoding="utf-8"
    )

    async def _fake_build(git_url, name, log_lines, branch="main", **kwargs):
        return {"ok": True, "pkg_dir": tmp_path}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_build)

    async def _fake_kpg(proc):
        proc.returncode = -9

    monkeypatch.setattr(registry, "_kill_process_group", _fake_kpg)
    proc = make_proc()

    async def _fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    await registry.install_from_registry("demoapp")

    events = [
        c.kwargs
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("operation") == "app_install_script"
    ]
    assert [e["outcome"] for e in events] == ["started", expected[0]]
    assert events[1]["resources"].endswith(expected[1])
    # Script output is never written into the audit record.
    assert all("secret-output" not in str(e) for e in events)
