"""The gateway boot matrix installs a supported Node before it boots a gateway.

A harness gateway that finds a Node below the supported major runs the boot-time
repair (``kiro_crew.cli._ensure_node``), and on POSIX that is ``ensure-node.sh``
downloading mise and a Node release. That download then sits inside the
KIROCREW_READY window and times the boot out on a slow network. The job
must therefore put a supported Node on PATH itself, before the boot step, on every
OS: Linux and macOS through their own setup-node step, Windows through
``setup-windows-tests``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from kiro_crew.constants import MIN_NODE_VERSION

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SETUP_NODE = "actions/setup-node@"
_BOOT_TEST = "test/e2e/test_gateway_boot_matrix.py"


def _steps() -> list[dict]:
    jobs = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))[
        "jobs"
    ]
    return jobs["e2e-boot-matrix"]["steps"]


def _boot_index(steps: list[dict]) -> int:
    return next(i for i, step in enumerate(steps) if _BOOT_TEST in step.get("run", ""))


def test_posix_legs_install_a_supported_node_before_the_boot() -> None:
    steps = _steps()
    boot = _boot_index(steps)
    node = [
        (i, step)
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith(_SETUP_NODE)
    ]
    assert len(node) == 1, "e2e-boot-matrix needs exactly one setup-node step"
    index, step = node[0]
    assert index < boot
    # Runs on every leg except Windows, which gets Node from setup-windows-tests.
    assert step.get("if") == "runner.os != 'Windows'"
    assert int(str(step["with"]["node-version"])) >= MIN_NODE_VERSION[0]
    # The CodeBuild seed comes right before it, so setup-node skips the GitHub
    # API manifest lookup, the same pairing every other setup-node lane uses.
    seed = steps[index - 1]
    assert seed.get("uses") == "./.github/actions/seed-node-tool-cache"
    assert seed.get("if") == step.get("if")


def test_windows_leg_gets_its_node_from_setup_windows_tests() -> None:
    steps = _steps()
    boot = _boot_index(steps)
    windows = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses") == "./.github/actions/setup-windows-tests"
    )
    assert windows < boot
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/setup-windows-tests/action.yml").read_text(encoding="utf-8")
    )["runs"]["steps"]
    pins = [
        int(str(step["with"]["node-version"]))
        for step in action
        if str(step.get("uses", "")).startswith(_SETUP_NODE)
    ]
    assert pins and all(pin >= MIN_NODE_VERSION[0] for pin in pins)
