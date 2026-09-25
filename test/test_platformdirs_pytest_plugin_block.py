"""The `platformdirs` pytest11 block, pinned from outside setup.cfg.

`platformdirs` declares a `pytest11` entry point exporting the
`platformdirs_isolated` fixture, so pytest imports `platformdirs.pytest_plugin`
during startup for every invocation in this repository. A `pytest11` entry point
that fails to import is fatal before collection: the run exits 1 with zero tests
and an annotation that says only `exit code 1`. `setup.cfg` refuses the plugin
with `-p no:platformdirs`, which pluggy honors BEFORE it imports the module.

The block is only safe while nothing here wants the fixture, and it is only
load-bearing while it is actually in `addopts`. Both halves are asserted here,
because neither is visible from any test that merely runs under pytest -- a
commit that drops the line leaves every local run green on whatever
`platformdirs` version the developer happens to have installed, and the loss
surfaces only as a CI lane dying before collection.
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
from pathlib import Path

from kiro_crew.subprocess_utf8 import UTF8_TEXT

_REPO = Path(__file__).resolve().parents[1]
_SETUP_CFG = _REPO / "setup.cfg"

_BLOCK = "-p no:platformdirs"
_PLUGIN_MODULE = "platformdirs.pytest_plugin"
_FIXTURE = "platformdirs_isolated"


def _addopts() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read_string(_SETUP_CFG.read_text(encoding="utf-8"))
    raw = parser.get("tool:pytest", "addopts")
    return [line.strip() for line in raw.splitlines() if line.strip()]


class TestTheBlockIsConfigured:
    def test_setup_cfg_refuses_the_platformdirs_plugin(self) -> None:
        assert _BLOCK in _addopts(), (
            f"setup.cfg [tool:pytest] addopts lost {_BLOCK!r}. Without it pytest imports "
            f"{_PLUGIN_MODULE} at startup, and an import error there ends the run before "
            "collection."
        )

    def test_the_running_session_has_the_block_applied(self, pytestconfig) -> None:
        """Proves the configured option reaches a live session, not just the file."""
        assert _BLOCK.split()[1] in pytestconfig.getoption("plugins"), (
            "the running pytest session did not receive `no:platformdirs`, so the "
            "setup.cfg entry is not taking effect"
        )


class TestTheBlockCostsNothing:
    def test_no_test_asks_for_the_isolated_fixture(self) -> None:
        """Blocking the plugin is only safe while the fixture is unused."""
        roots = [_REPO / "test", _REPO / "src" / "kiro_crew" / "apps" / "builtins"]
        users = [
            path
            for root in roots
            for path in root.rglob("*.py")
            if path != Path(__file__) and _FIXTURE in path.read_text(encoding="utf-8")
        ]
        assert not users, (
            f"{_FIXTURE} is requested by {[str(p.relative_to(_REPO)) for p in users]}, so "
            f"{_BLOCK!r} in setup.cfg would make those tests error on a missing fixture"
        )

    def test_the_plugin_is_absent_from_the_running_session(self) -> None:
        """The block takes effect: the plugin module is never imported."""
        assert (
            sys.modules.get(_PLUGIN_MODULE) is None
        ), f"{_PLUGIN_MODULE} is imported in this session, so the block is not holding"


class TestBlockingSurvivesAnUnimportablePlugin:
    """The behaviour the block exists for, exercised end to end in a subprocess.

    A declared-but-unimportable `pytest11` entry point is what kills a lane. This
    builds exactly that shape in a throwaway tree -- a distribution whose
    `entry_points.txt` names a module that does not exist -- and shows the block
    is what separates a dead run from a passing one.
    """

    @staticmethod
    def _tree(tmp_path: Path) -> tuple[Path, dict[str, str]]:
        site = tmp_path / "site"
        dist = site / "brokenplug-1.0.0.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: brokenplug\nVersion: 1.0.0\n", encoding="utf-8"
        )
        (dist / "entry_points.txt").write_text(
            "[pytest11]\nbrokenplug = brokenplug.missing_module\n", encoding="utf-8"
        )
        # The package imports, the plugin submodule does not -- the runner shape.
        pkg = site / "brokenplug"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")

        project = tmp_path / "project"
        project.mkdir()
        (project / "test_probe.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
        return project, {"PYTHONPATH": str(site)}

    def _run(self, project: Path, env: dict[str, str], *extra: str) -> subprocess.CompletedProcess:
        # _BLOCK is passed to the CHILD as well, and it is not the thing under
        # test here: `brokenplug` is. The child runs in a throwaway tree with
        # PYTEST_ADDOPTS cleared, so setup.cfg cannot reach it, and on a host
        # whose own platformdirs plugin will not import the child would die on
        # that instead of on the synthetic subject. Blocking it keeps
        # `brokenplug` the only variable, so both directions below mean what
        # they say.
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                *_BLOCK.split(),
                "-q",
                *extra,
                "test_probe.py",
            ],
            cwd=project,
            env={
                **os.environ,
                **env,
                "PYTEST_ADDOPTS": "",
                "PYTHONIOENCODING": "utf-8",
            },
            capture_output=True,
            timeout=180,
            **UTF8_TEXT,
        )

    def test_an_unimportable_entry_point_kills_the_run(self, tmp_path: Path) -> None:
        project, env = self._tree(tmp_path)
        result = self._run(project, env)
        assert result.returncode != 0
        assert "brokenplug.missing_module" in result.stdout + result.stderr

    def test_the_block_lets_the_same_run_pass(self, tmp_path: Path) -> None:
        project, env = self._tree(tmp_path)
        result = self._run(project, env, "-p", "no:brokenplug")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
