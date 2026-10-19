"""Fixtures and helpers shared by ``test_pod.py`` and its nine sibling modules.

Importing the three autouse fixtures gives a module this host-independent setup:
``_isolate_pod_host_state`` points ``HOME`` and ``USERPROFILE`` at a scratch directory and
clears the rootdir conftest's ``KIROCREW_POD_ROOT`` and ``KIROCREW_POD_ENV_DIR`` pins,
``_fake_node_toolchain`` resolves node tools under ``NODE_BIN``, and ``_systemd_backend``
selects the systemd backend on every host. ``cfg_fixture`` is the ``cfg`` fixture
(``PodConfig.load()``), ``_cp`` builds a ``CompletedProcess``, ``_ready_worktree`` builds a
flat checkout, and ``requires_posix_pod_lifecycle`` skips a test off POSIX.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig

# Stand-in for a version-manager node bin dir (mise/nvm/fnm/volta/asdf install
# under $HOME). Provisioning resolves ``npm`` to an absolute path there.
NODE_BIN = "/fake/node/bin"

requires_posix_pod_lifecycle = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="pod lifecycle requires POSIX descriptor traversal",
)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse node-toolchain pin moved verbatim; no test in the modules that import it calls monkeypatch.undo()
def _fake_node_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin node-toolchain resolution so provisioning tests are host-independent.

    ``provision`` now resolves ``npm`` to an absolute path and hands the child a
    PATH carrying the node bin dir (npm run-scripts are ``#!/usr/bin/env node``).
    Without this fixture the provisioning tests would pass or fail according to whether
    the host running them happens to have a version manager installed.
    """
    monkeypatch.setattr(prov, "find_node_tool", lambda name: f"{NODE_BIN}/{name}")
    monkeypatch.setattr(
        prov, "node_augmented_path", lambda base="": f"{NODE_BIN}{os.pathsep}{base}"
    )


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse host-home pin moved verbatim; no test in the modules that import it calls monkeypatch.undo()
def _isolate_pod_host_state(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pod HOST state out of the developer's real home.

    Two pod paths resolve through ``Path.home()`` rather than ``KIROCREW_HOME``, so
    conftest's safety net does not cover them, and both are written by ordinary
    test paths:

    ``unit.unit_path`` — a test reaching ``install_unit`` (via ``_up`` ->
    ``install_backend``, or the ``start_pod`` self-heal) rewrites the REAL
    ``kirocrew-pod@.service`` with this test's tmpdir as
    ``Environment=KIROCREW_POD_ROOT=``. Every later ``pod up`` on the host then dies
    with "no pinned checkout", so running the suite breaks pods for everyone until
    someone re-runs ``pod install``. Observed on a real host.

    ``PodConfig.pods_dir`` — resolves off the DEFAULT data home on purpose (a pod
    process must not be able to redirect the host's pod registry into its own
    throwaway home). The per-name lifecycle mutex writes its lock file there.

    Pinning ``HOME`` fixes the cause both share instead of patching each function
    that reads it, so a future path that resolves through the home is covered
    without a matching change here. ``USERPROFILE`` too, because Windows
    ``Path.home()`` reads that one. Tests that pin either themselves still win.

    The rootdir conftest now ALSO pins ``KIROCREW_POD_ROOT`` / ``KIROCREW_POD_ENV_DIR``
    for every test (the same leak, found again from a module without this fixture).
    This fixture clears those two pins on purpose: the modules that import it test the
    home-DERIVED defaults (``test_pod_systemd.py``'s ``TestHostStateIsFenced`` asserts the
    pod dirs follow the pinned home), and with ``HOME`` redirected above the derivation is
    already hermetic. Tests that set the variables themselves are unaffected.
    """
    home = tmp_path_factory.mktemp("pod-host-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("KIROCREW_POD_ROOT", raising=False)
    monkeypatch.delenv("KIROCREW_POD_ENV_DIR", raising=False)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse systemd-backend pin moved verbatim; no test in the modules that import it calls monkeypatch.undo()
def _systemd_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the SYSTEMD backend by default, on every host.

    ``runtime`` dispatches unit operations on ``IS_MACOS`` and ``IS_WINDOWS``,
    so without this the tests that monkeypatch ``rt.systemctl`` would silently
    exercise the launchd branch on a Mac or the Task Scheduler branch on the
    Windows shards — passing on Linux CI and failing (or worse, vacuously
    passing) elsewhere; a Windows run of the modules that import it fails
    exactly that way without it. Pinning both makes the
    default explicit and keeps the systemd contract asserted everywhere. Tests
    for the macOS or Windows path set their flag True themselves.
    """
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt, "IS_WINDOWS", False)


@pytest.fixture(name="cfg")
def cfg_fixture() -> PodConfig:
    return PodConfig.load()


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _ready_worktree(
    root: Path, name: str, *, venv: bool = True, dist: bool = True, flags: bool = True
) -> Path:
    """Build a flat kirocrew worktree checkout (repo root == worktree dir).

    ``flags`` writes a ``cli.py`` declaring the gateway flags a CURRENT checkout
    declares. ``boot`` probes the target's own source before passing a flag to it
    (the control plane and the target worktree are independent checkouts), so a
    fixture without it models an OLD checkout -- pass ``flags=False`` to exercise
    that arm deliberately rather than by omission.
    """
    co = root / name
    co.mkdir(parents=True, exist_ok=True)
    if venv:
        # Mirror the real per-platform venv layout so the detectors are
        # exercised against what a venv on THIS host actually looks like.
        b = prov.venv_bin(co)
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    if dist:
        (co / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True, exist_ok=True)
    if flags:
        cli = co / "src" / "kiro_crew" / "cli.py"
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text('gw.add_argument("--no-crons")\ngw.add_argument("--no-tunnel")\n')
    return co
