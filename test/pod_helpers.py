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
def _fake_node_toolchain(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin node-toolchain resolution so provisioning tests are host-independent.

    ``provision`` now resolves ``npm`` to an absolute path and hands the child a
    PATH carrying the node bin dir (npm run-scripts are ``#!/usr/bin/env node``).
    Without this fixture the provisioning tests would pass or fail according to whether
    the host running them happens to have a version manager installed.
    """
    _floor_monkeypatch.setattr(prov, "find_node_tool", lambda name: f"{NODE_BIN}/{name}")
    _floor_monkeypatch.setattr(
        prov, "node_augmented_path", lambda base="": f"{NODE_BIN}{os.pathsep}{base}"
    )


@pytest.fixture(autouse=True)
def _isolate_pod_host_state(tmp_path_factory, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pod HOST state out of the developer's real home.

    ``unit.unit_path`` and ``PodConfig.pods_dir`` resolve through ``Path.home()``,
    not ``KIROCREW_HOME``; an unpinned ``install_unit`` rewrites the host's real
    ``kirocrew-pod@.service``. Pins ``HOME`` and ``USERPROFILE`` (Windows reads that).

    Clears the rootdir conftest's ``KIROCREW_POD_ROOT`` / ``KIROCREW_POD_ENV_DIR`` pins
    so these modules test the home-derived defaults, which the ``HOME`` pin keeps
    hermetic. Tests that set any of these themselves still win.
    """
    home = tmp_path_factory.mktemp("pod-host-home")
    _floor_monkeypatch.setenv("HOME", str(home))
    _floor_monkeypatch.setenv("USERPROFILE", str(home))
    _floor_monkeypatch.delenv("KIROCREW_POD_ROOT", raising=False)
    _floor_monkeypatch.delenv("KIROCREW_POD_ENV_DIR", raising=False)


@pytest.fixture(autouse=True)
def _systemd_backend(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the SYSTEMD backend by default, on every host.

    ``runtime`` dispatches on ``IS_MACOS`` and ``IS_WINDOWS``, so tests patching
    ``rt.systemctl`` would otherwise run the launchd or Task Scheduler branch. Tests
    for those paths set their flag True themselves.
    """
    _floor_monkeypatch.setattr(rt, "IS_MACOS", False)
    _floor_monkeypatch.setattr(rt, "IS_WINDOWS", False)


@pytest.fixture(name="cfg")
def cfg_fixture() -> PodConfig:
    return PodConfig.load()


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _ready_worktree(
    root: Path, name: str, *, venv: bool = True, dist: bool = True, flags: bool = True
) -> Path:
    """Build a flat kirocrew worktree checkout (repo root == worktree dir).

    ``flags`` writes a ``cli.py`` declaring the current gateway flags; ``boot`` probes
    the target's source before passing one. ``flags=False`` models an old checkout.
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
