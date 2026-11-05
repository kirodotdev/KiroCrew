"""Spec Builder's pins of the host primitives it relies on.

Each case is the app's row of a core contract whose other rows stay in core:

- ``fd_real_path`` is one object, defined in ``pinned_fs``
  (``test_pinned_staging_helper_contract.py``): the app's descriptor-pinned reads
  in ``repository.py`` bind that object rather than a fork of it.
- A reader that cannot import ``kiro_crew.security`` denies
  (``test_security_single_storage.py``): the app's parser treats every path as
  sensitive when the security module is unavailable.
- Path containment folds Windows' extended-length prefix off both operands
  (``test_windows_extended_prefix_containment.py``): ``repository._contained``
  accepts the prefixed spelling of an inside path and still refuses an outside one.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from kiro_crew import pinned_fs
from kiro_crew.apps.builtins.spec_builder.backend import parsers, repository

SECURITY_PACKAGE = "kiro_crew.security"
_PREFIX = "\\\\?\\"


def test_the_repository_binds_the_pinned_fs_fd_real_path():
    # The app's descriptor-pinned reads live in repository.py (the handlers
    # refactor moved them out of routes.py).
    assert repository.fd_real_path is pinned_fs.fd_real_path


def test_the_parser_still_fails_closed_without_the_security_module() -> None:
    """The guard an unresolvable security import raises for: a reader that cannot
    import it denies.

    With no security module the parser treats every path as sensitive rather than
    waving them through. Pinned on the SHAPE of that guard, not on its wording: the
    handler has to catch the class an import failure raises, and the fallback gate
    has to answer ``True``. An edit that narrowed either fails here.
    """
    source = Path(parsers.__file__ or "")
    tree = ast.parse(source.read_text(encoding="utf-8"))

    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(
            isinstance(stmt, ast.ImportFrom) and stmt.module == SECURITY_PACKAGE
            for stmt in ast.walk(node)
        )
    ]
    assert guarded, f"the parser imports {SECURITY_PACKAGE} without a guard"

    def _catches_import_failure(handler: ast.ExceptHandler) -> bool:
        names: list[str] = []
        if isinstance(handler.type, ast.Name):
            names = [handler.type.id]
        elif isinstance(handler.type, ast.Tuple):
            names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
        return bool({"ImportError", "Exception", "BaseException"} & set(names))

    handlers = [h for node in guarded for h in node.handlers]
    assert any(_catches_import_failure(h) for h in handlers), (
        "no handler catches the class an unresolvable security import raises, so "
        "the failure would propagate past the fail-closed fallback"
    )

    fallbacks = [
        node
        for handler in handlers
        for node in ast.walk(handler)
        if isinstance(node, ast.FunctionDef) and node.name == "is_sensitive_path"
    ]
    assert fallbacks, "the guard installs no fail-closed is_sensitive_path fallback"
    returns = [
        node.value.value
        for fallback in fallbacks
        for node in ast.walk(fallback)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, bool)
    ]
    assert returns and set(returns) == {True}, (
        "the no-security fallback no longer answers True for every path, so an "
        f"unresolvable gate would stop denying: {returns}"
    )


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A root with one file inside it, and a sibling directory outside it."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    inside = root / "inside.txt"
    inside.write_text("x", encoding="utf-8")
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "f.txt").write_text("x", encoding="utf-8")
    return root, inside, outside / "f.txt"


def test_containment_folds_the_windows_extended_length_prefix(tree, monkeypatch) -> None:
    root, inside, outside = tree
    real_realpath = os.path.realpath

    def _prefixed_realpath(path, *a, **k):  # type: ignore[no-untyped-def]
        out = real_realpath(path, *a, **k)
        return _PREFIX + out if out == str(inside) else out

    monkeypatch.setattr(os.path, "realpath", _prefixed_realpath)
    assert repository._contained(inside, root)
    assert not repository._contained(outside, root)
