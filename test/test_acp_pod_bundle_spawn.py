"""The pod child spawns the kiro-cli BUNDLE binary, wrapped in Crew's sandbox.

The pod ``HOME`` remap (``acp.client._apply_pod_home_remap``) relocates kiro-cli's
OAuth grant tree into the pod, but the installed ``kiro-cli`` is usually a shim
that prefers ``aim sandbox``, whose mount plan is built around the REAL user home
and fails to construct under a remapped one -- the child dies before kiro-cli
starts. These tests pin the two halves of the fix as ONE decision
(``apply_pod_bundle_spawn``): which binary runs, and who sandboxes it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.acp.client import (
    _kiro_cli_bundle_binary,
    apply_pod_bundle_spawn,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_POD_HOME_REMAP,
)


def _bundle_layout(tmp_path: Path) -> tuple[str, str]:
    """A toolbox-shaped install: ``<root>/sandbox/kiro-cli`` shim + ``<root>/kiro-cli``.

    Mirrors the real layout the shim's own fallback resolves
    (``<bundle root>/kiro-cli`` from ``<bundle root>/sandbox/kiro-cli``).
    """
    root = tmp_path / "tools" / "kiro-cli" / "9.9.9"
    (root / "sandbox").mkdir(parents=True)
    shim = root / "sandbox" / "kiro-cli"
    bundle = root / "kiro-cli"
    for path in (shim, bundle):
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return str(shim), str(bundle)


def _pod_env(os_home: Path) -> dict[str, str]:
    return {"KIROCREW_POD": "1", "KIROCREW_OS_HOME": str(os_home)}


# ── The decision inside a pod ──────────────────────────────────────────────────


def test_pod_spawn_swaps_shim_for_bundle_binary_and_takes_crew_sandbox(
    tmp_path: Path,
) -> None:
    """The regression this fixes: a pod child must not run the toolbox shim.

    Red before the fix -- the pod child kept the shim (whose sandbox EBUSYs under
    the remapped HOME) and Crew skipped its own launcher for it.
    """
    shim, bundle = _bundle_layout(tmp_path)
    argv, delegate = apply_pod_bundle_spawn(
        [shim, "acp", "--agent", "kirocrew"],
        backend=ACP_BACKEND_KIRO,
        environ=_pod_env(tmp_path / "os-home"),
    )
    assert argv == [bundle, "acp", "--agent", "kirocrew"]
    # The shim is bypassed, so kiro-cli's internal sandbox never runs: Crew's
    # launcher must wrap the child instead of delegating to it.
    assert delegate is False


def test_pod_spawn_honors_a_preset_kiro_cli_path_like_the_shim_does(
    tmp_path: Path,
) -> None:
    """``KIRO_CLI_PATH`` wins outright -- the shim honors it before computing."""
    shim, _bundle = _bundle_layout(tmp_path)
    pinned = tmp_path / "pinned-kiro-cli"
    pinned.write_text("#!/bin/sh\nexit 0\n")
    pinned.chmod(0o755)
    env = _pod_env(tmp_path / "os-home") | {"KIRO_CLI_PATH": str(pinned)}
    argv, delegate = apply_pod_bundle_spawn([shim, "acp"], backend=ACP_BACKEND_KIRO, environ=env)
    assert argv == [str(pinned), "acp"]
    assert delegate is False


def test_pod_spawn_degrades_to_status_quo_when_no_bundle_binary_exists(
    tmp_path: Path,
) -> None:
    """A host with no toolbox bundle keeps the shim rather than an unlaunchable path.

    A pod whose child cannot bootstrap is meant to be refused loudly at ``pod
    up``; this seam must not invent a path that does not exist.
    """
    lone = tmp_path / "bin" / "kiro-cli"
    lone.parent.mkdir(parents=True)
    lone.write_text("#!/bin/sh\nexit 0\n")
    lone.chmod(0o755)
    argv, delegate = apply_pod_bundle_spawn(
        [str(lone), "acp"],
        backend=ACP_BACKEND_KIRO,
        environ=_pod_env(tmp_path / "os-home"),
    )
    assert argv == [str(lone), "acp"]
    assert delegate is (ACP_BACKEND_KIRO in ACP_BACKENDS_INTERNAL_SANDBOX)


# ── The non-pod path is byte-identical ─────────────────────────────────────────


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="no-pod-marker"),
        pytest.param({"KIROCREW_POD": "0"}, id="marker-0"),
        pytest.param({"KIROCREW_POD": "false"}, id="marker-false"),
        pytest.param({"KIROCREW_POD": "1"}, id="marker-without-os-home"),
    ],
)
def test_non_pod_spawn_argv_and_wrap_decision_are_unchanged(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    """Outside a pod the function is a no-op, including for truthy-but-not-"1" markers.

    ``KIROCREW_POD`` is compared EXACTLY to ``"1"`` (every non-empty string is
    truthy in Python), and a marker with no ``KIROCREW_OS_HOME`` means no remap
    happened, so there is nothing to compensate for.
    """
    shim, _bundle = _bundle_layout(tmp_path)
    original = [shim, "acp", "--agent", "kirocrew"]
    argv, delegate = apply_pod_bundle_spawn(
        list(original), backend=ACP_BACKEND_KIRO, environ=environ
    )
    assert argv == original
    assert delegate is (ACP_BACKEND_KIRO in ACP_BACKENDS_INTERNAL_SANDBOX)


def test_a_harness_outside_the_remap_set_is_untouched_inside_a_pod(tmp_path: Path) -> None:
    """Gated on the remap set, never on a backend negation (harness-parity H6/H7).

    A harness whose credentials do not follow ``$HOME`` gets no remap, so it must
    get no bundle substitution either -- and it keeps whatever sandbox ownership
    its own set membership says.
    """
    shim, _bundle = _bundle_layout(tmp_path)
    other = "kas"
    assert other not in ACP_BACKENDS_POD_HOME_REMAP
    original = [shim, "--acp"]
    argv, delegate = apply_pod_bundle_spawn(
        list(original), backend=other, environ=_pod_env(tmp_path / "os-home")
    )
    assert argv == original
    assert delegate is (other in ACP_BACKENDS_INTERNAL_SANDBOX)


# ── Bundle resolution ─────────────────────────────────────────────────────────


def test_bundle_resolution_returns_none_when_already_the_bundle_binary(
    tmp_path: Path,
) -> None:
    """No pointless swap: the candidate resolving back to the input means no shim."""
    _shim, bundle = _bundle_layout(tmp_path)
    assert _kiro_cli_bundle_binary(bundle, environ={}) is None


def test_bundle_resolution_follows_the_symlink_chain(tmp_path: Path) -> None:
    """The installed name is typically a symlink into the versioned bundle."""
    shim, bundle = _bundle_layout(tmp_path)
    link = tmp_path / "local-bin-kiro-cli"
    link.symlink_to(shim)
    assert _kiro_cli_bundle_binary(str(link), environ={}) == bundle


@pytest.mark.skipif(
    os.name == "nt",
    reason="os.access(X_OK) is vacuous on Windows (any existing file passes); pods are "
    "systemd/launchd-only, so no Windows resolution semantics are invented here",
)
def test_bundle_resolution_rejects_a_non_executable_candidate(tmp_path: Path) -> None:
    """A present-but-unexecutable candidate is not a spawnable answer.

    POSIX-only by CAPABILITY, not by preference: ``os.access(path, X_OK)`` answers
    True for any existing file on Windows, so the executability half of the
    resolver's contract is unobservable there. Pods are systemd/launchd-only, so the
    honest move is to gate this branch rather than invent a PATHEXT rule the
    production resolver does not implement.
    """
    shim, bundle = _bundle_layout(tmp_path)
    os.chmod(bundle, 0o644)
    assert _kiro_cli_bundle_binary(shim, environ={}) is None
