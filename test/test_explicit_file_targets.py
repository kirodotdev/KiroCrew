"""The local gate's explicit file arguments collect without the sibling walk.

``scripts/run_scoped_tests.py`` hands pytest its related set as explicit file paths,
and pytest re-collects a file argument's directory once per argument, offering every
sibling to ``pytest_ignore_collect`` and ``pytest_collect_file`` each time. The
runner loads ``scripts.explicit_file_targets``, which answers the ignore hook first
for every path pytest asks about. These tests pin that the real runner argv offers
pytest nothing but its targets, that the plugin changes no node ID, and that it
refuses the one argument shape it would empty.

The three nested pytest children start once per module, one after another, from a
module-scoped fixture in this module's own ``xdist_group``; every test reads their
results. Each child gets its own ``--basetemp`` under the fixture's temporary directory
and runs with ``-n0`` (testing-conventions: a nested pytest always gets both).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from kiro_crew.testing.wait import default_timeout

# One xdist worker for the whole module: the module-scoped ``children`` fixture starts
# three real interpreters, and under ``--dist loadgroup`` an unmarked module spread
# across workers would start them once per worker.
pytestmark = pytest.mark.xdist_group(name="explicit_file_targets_children")

ROOT = Path(__file__).resolve().parents[1]
# One flat ``test/`` module with parametrized cases, one under a subdirectory with its own
# conftest, one inside an app's ``tests`` package: the three directory shapes the walk meets.
TARGETS = (
    "test/test_platform_compat.py",
    "test/e2e/scenarios/test_settings_save.py",
    "src/kiro_crew/apps/builtins/auto_improvement/tests/test_bug_proposal_skip_status_default.py",
)
PLUGIN = "scripts.explicit_file_targets"

_FILE_COUNTER_PLUGIN = """
import os

def pytest_collect_file(file_path, parent):
    with open(os.environ["COLLECT_FILE_LOG"], "a", encoding="utf-8") as fh:
        fh.write(str(file_path) + "\\n")
"""


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "COV_CORE_SOURCE",
        "COV_CORE_DATAFILE",
        "COV_CORE_CONFIG",
        "COVERAGE_PROCESS_START",
        "KIROCREW_HOME",
    ):
        env.pop(key, None)
    env.update(
        PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT))),
        PYTHONDONTWRITEBYTECODE="1",
        COVERAGE_FILE=str(root / ".coverage"),
        TMPDIR=str(root),
        TMP=str(root),
        TEMP=str(root),
    )
    return env


def _collect_only(basetemp: Path, *options: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-n0",
        "--no-cov",
        "--collect-only",
        "-qq",
        "--color=no",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(basetemp),
        *options,
    ]


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )


def _node_ids(result: subprocess.CompletedProcess[str]) -> list[str]:
    assert result.returncode == 0, result.stdout + result.stderr
    nodes = [line for line in result.stdout.splitlines() if "::" in line]
    assert nodes and len(nodes) == len(set(nodes)), result.stdout
    return nodes


@dataclass(frozen=True)
class Children:
    """The module's three nested pytest runs, started once and read by every test."""

    runner: subprocess.CompletedProcess[str]
    offered: set[Path]
    plain: subprocess.CompletedProcess[str]
    refusal: subprocess.CompletedProcess[str]


def _runner_argv(root: Path) -> list[str]:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from run_scoped_tests import backend_argv
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    argv = backend_argv(list(TARGETS))
    # Options go before the runner's ``--``; a later ``-n0`` overrides its ``-n auto``.
    # The runner already passes ``-q``; one more makes the plain child's ``-qq``, which
    # prints node IDs (``-qqq`` would print per-file counts instead).
    argv[argv.index("--") : argv.index("--")] = [
        "-n0",
        "--collect-only",
        "-q",
        "--color=no",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(root / "bt-runner"),
        "-p",
        "count_collect_files",
    ]
    return argv


@pytest.fixture(scope="module")
def children(tmp_path_factory: pytest.TempPathFactory) -> Children:
    """Run the three children serially, once for the module."""
    root = tmp_path_factory.mktemp("explicit_file_targets")
    env = _env(root)
    (root / "plugin").mkdir()
    (root / "plugin" / "count_collect_files.py").write_text(_FILE_COUNTER_PLUGIN, "utf-8")
    log = root / "collected-files.txt"
    runner_env = dict(env)
    runner_env["PYTHONPATH"] = os.pathsep.join((str(root / "plugin"), env["PYTHONPATH"]))
    runner_env["COLLECT_FILE_LOG"] = str(log)
    runner = _run(_runner_argv(root), runner_env)
    offered = (
        {Path(line).resolve() for line in log.read_text("utf-8").splitlines()}
        if log.exists()
        else set()
    )
    plain = _run(_collect_only(root / "bt-plain", *TARGETS), env)
    refusal = _run(_collect_only(root / "bt-refusal", "-p", PLUGIN, "test/e2e/scenarios"), env)
    return Children(runner=runner, offered=offered, plain=plain, refusal=refusal)


def test_the_runner_argv_offers_pytest_only_its_targets(children: Children) -> None:
    """``backend_argv``'s own pytest command reads the targets and not one sibling."""
    result = children.runner
    assert result.returncode == 0, result.stdout + result.stderr
    assert children.offered == {(ROOT / target).resolve() for target in TARGETS}


def test_the_plugin_collects_the_same_node_ids_as_the_plain_walk(children: Children) -> None:
    """Same node IDs, same order, with and without the plugin."""
    plain_nodes = _node_ids(children.plain)
    filtered_nodes = _node_ids(children.runner)
    assert filtered_nodes == plain_nodes
    assert {node.split("::", 1)[0] for node in plain_nodes} == set(TARGETS)


def test_the_hook_answers_only_while_pytest_collects(tmp_path: Path) -> None:
    """Outside the walk, a test asking the hook chain about a path gets the chain's answer.

    ``test_platform_collect_ignore.py`` and ``test_local_gate.py`` offer paths to the live
    chain and expect the platform list's verdict; under the runner this plugin is loaded,
    and it must not answer for them.
    """
    from scripts import explicit_file_targets as plugin

    sibling = tmp_path / "test_sibling.py"
    assert plugin.pytest_ignore_collect(sibling) is None
    walk = plugin.pytest_collection(session=None)
    next(walk)
    assert plugin.pytest_ignore_collect(sibling) is True
    try:
        walk.send(None)
    except StopIteration:
        pass
    assert plugin.pytest_ignore_collect(sibling) is None


def test_a_directory_argument_is_a_usage_error(children: Children) -> None:
    """The plugin would collect nothing under a directory, so it refuses to start."""
    result = children.refusal
    assert result.returncode == 4, result.stdout + result.stderr
    assert "collects only file arguments" in result.stderr
    assert "test/e2e/scenarios" in result.stderr
