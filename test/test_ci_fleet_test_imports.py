"""Fleet contract tests share modules even when Python ships its own test package."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "first_module",
    [
        "test_ci_additional_fleet_routes",
        "test_ci_windows_canary",
        "test_ci_nonroot_boundary",
    ],
)
def test_shared_imports_with_stdlib_test_package(tmp_path, first_module):
    shadow = tmp_path / "stdlib" / "test"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            """
import importlib
import sys
from pathlib import Path

root, shadow = map(Path, sys.argv[1:3])
# Match pytest's prepend mode while keeping a real regular test package later.
sys.path[:0] = [str(root / 'test'), str(root / 'src'), str(root), str(shadow.parent)]
stdlib_test = importlib.import_module('test')
assert Path(stdlib_test.__file__).resolve() == (shadow / '__init__.py').resolve()
first = importlib.import_module(sys.argv[3])
if sys.argv[3] == 'test_ci_nonroot_boundary':
    jobs = first.yaml.safe_load((root / '.github/workflows/ci.yml').read_text(encoding='utf-8'))['jobs']
    first.test_the_boundary_follows_setup_and_covers_every_test_and_coverage_step(jobs, 'e2e-boot-matrix')
routes = importlib.import_module('test_ci_additional_fleet_routes')
windows = importlib.import_module('test_ci_windows_canary')
parity = importlib.import_module('test_ci_fleet_routing_expression_parity')
assert routes.parity is parity
assert windows._expression is routes._evaluate
assert routes._evaluate("'' || 'hosted'", {}) == 'hosted'
assert sys.modules['test'] is stdlib_test
""",
            str(_REPO_ROOT),
            str(shadow),
            first_module,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "test_ci_additional_fleet_routes",
        "test_ci_windows_canary",
        "test_ci_nonroot_boundary",
        "test_gateway_lock_diagnosis",
    ],
)
def test_shared_imports_do_not_depend_on_test_package(module):
    tree = ast.parse((_REPO_ROOT / "test" / f"{module}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module != "test" and not node.module.startswith(
                "test."
            ), f"{module}:{node.lineno}: use pytest-prepended sibling imports"
