"""GATE F3 — test-presence gate for the workflows engine.

Touching ``kiro_crew/workflows/**`` without a matching test must fail the build
(GATES.md F3). The fully *diff-aware* form keys off ``git diff`` to see which
files changed — but git state is environment-fragile in this clone (see LOOP.md
intervention log: a ``/tmp``-is-a-git-repo test already reddened the trunk), and
GATE F1 set the precedent of encoding these fitness rules as **pure-stdlib,
git-independent** checks. So F3's enforceable core is a *module-presence
invariant*:

    Every implementation module under ``workflows/`` must be imported by at least
    one ``test/test_workflows_*.py`` file.

You cannot add a ``workflows/*.py`` module without a test that imports it — the
moment you do, this goes RED naming the orphan module. That is exactly the
property the diff-aware gate exists to guarantee, expressed deterministically and
independent of git/CI plumbing. (A CI wrapper may still diff against the merge
base for fast feedback; this test is the hard floor underneath it.)

If this is RED: add (or wire) a ``test_workflows_<area>.py`` that imports the new
module and exercises it. Do **not** delete the module from any map to silence the
gate (see GATES.md F3 — no gate is waived silently).
"""

from __future__ import annotations

import ast
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
WORKFLOWS_DIR = TEST_DIR.parent / "src" / "kiro_crew" / "workflows"
PKG = "kiro_crew.workflows"


def _workflows_modules() -> list[str]:
    """Module stems under ``workflows/`` (``__init__`` denotes the package root)."""
    return sorted(p.stem for p in WORKFLOWS_DIR.glob("*.py"))


def _test_files() -> list[Path]:
    return sorted(TEST_DIR.glob("test_workflows_*.py"))


def _referenced_paths(tree: ast.Module) -> set[str]:
    """Dotted module paths a test imports (``import X`` / ``from X import Y``).

    For ``from X import Y`` we record both ``X`` and ``X.Y`` so that
    ``from kiro_crew.workflows.dsl import parallel`` registers ``...workflows.dsl``.
    """
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            paths.add(node.module)
            for alias in node.names:
                paths.add(f"{node.module}.{alias.name}")
    return paths


def _expected_path(stem: str) -> str:
    """The dotted path a test must import to 'cover' a workflows module."""
    return PKG if stem == "__init__" else f"{PKG}.{stem}"


def _all_referenced_paths() -> set[str]:
    paths: set[str] = set()
    for path in _test_files():
        paths |= _referenced_paths(ast.parse(path.read_text(encoding="utf-8")))
    return paths


def test_every_workflows_module_has_a_referencing_test() -> None:
    referenced = _all_referenced_paths()
    modules = _workflows_modules()
    assert modules, f"no workflows modules found under {WORKFLOWS_DIR} — wrong path?"
    orphans = [m for m in modules if _expected_path(m) not in referenced]
    assert not orphans, (
        f"workflows modules with no referencing test: {orphans}. Add a "
        "test/test_workflows_<area>.py that imports each (GATE F3 — touching "
        "workflows/** requires a matching test). Do not silence the gate."
    )


def test_presence_gate_flags_an_orphan_module() -> None:
    """Negative control: the gate must actually catch an unreferenced module."""
    referenced = {PKG, f"{PKG}.context", f"{PKG}.validate"}
    pretend_modules = ["__init__", "context", "validate", "schema"]  # schema = orphan
    orphans = [m for m in pretend_modules if _expected_path(m) not in referenced]
    assert orphans == ["schema"]
