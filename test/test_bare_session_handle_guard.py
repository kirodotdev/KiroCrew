"""A test builds a constructor-less ``AcpSessionHandle`` only through ``bare_session_handle``.

A handle built with ``AcpSessionHandle.__new__`` carries only the fields its test set.
When a handle method later reads a new runtime attribute or awaits a new step, every such
handle raises ``AttributeError``, and a test added on a branch cut before that change
breaks main once both land. ``bare_session_handle`` (``test/session_handle_helpers.py``)
supplies those defaults in one place, so this guard refuses a direct
``AcpSessionHandle.__new__(...)`` or ``object.__new__(AcpSessionHandle)`` anywhere under
``test/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent
_CLASS = "AcpSessionHandle"

# Files allowed to build the handle directly, one per line, each with its reason.
_EXEMPT = {
    "session_handle_helpers.py",  # the factory itself
}


def _names_the_class(node: ast.AST | None) -> bool:
    # shortcut: an import alias (``AcpSessionHandle as H``) is not followed; resolve
    # ImportFrom aliases if one ever appears under test/.
    return (isinstance(node, ast.Name) and node.id == _CLASS) or (
        isinstance(node, ast.Attribute) and node.attr == _CLASS
    )


def _bare_builds(tree: ast.AST) -> list[int]:
    """Line numbers of ``AcpSessionHandle.__new__(...)`` and ``<x>.__new__(AcpSessionHandle)``."""
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "__new__":
            continue
        first = node.args[0] if node.args else None
        if _names_the_class(node.func.value) or _names_the_class(first):
            lines.append(node.lineno)
    return lines


def _offenders(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in _EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        if _CLASS not in source or "__new__" not in source:
            continue
        found.extend(f"{rel}:{line}" for line in _bare_builds(ast.parse(source)))
    return found


def test_no_test_builds_a_bare_session_handle_directly():
    offenders = _offenders(_TEST_ROOT)
    assert not offenders, (
        "Build a constructor-less AcpSessionHandle with "
        "`from session_handle_helpers import bare_session_handle`, not __new__:\n  "
        + "\n  ".join(offenders)
    )


def test_every_exemption_is_a_file_that_still_exists():
    assert [name for name in _EXEMPT if not (_TEST_ROOT / name).is_file()] == []


def test_the_guard_sees_both_spellings(tmp_path):
    (tmp_path / "test_direct.py").write_text(
        "h = AcpSessionHandle.__new__(AcpSessionHandle)\n", encoding="utf-8"
    )
    (tmp_path / "test_object.py").write_text(
        "x = 1\nh = object.__new__(AcpSessionHandle)\n", encoding="utf-8"
    )
    (tmp_path / "test_module.py").write_text(
        "h = session_handle.AcpSessionHandle.__new__(session_handle.AcpSessionHandle)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_other.py").write_text(
        "h = object.__new__(AcpSessionProvider)\n# AcpSessionHandle.__new__ in a comment\n",
        encoding="utf-8",
    )
    assert _offenders(tmp_path) == ["test_direct.py:1", "test_module.py:1", "test_object.py:2"]
