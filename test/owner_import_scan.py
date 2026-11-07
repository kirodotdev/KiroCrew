"""The owner-import scan the facade tests' ``test_only_the_family_imports_an_owner`` share.

A module outside a split family that imports an owner directly holds bindings the facade
never sees, so a patch through the facade misses it. Each facade test passes a predicate
on an owner's dotted name.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from pathlib import Path


def imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def owner_importers(
    sources: Iterable[tuple[Path, str]], root: Path, is_owner: Callable[[str], bool]
) -> set[str]:
    """The files among *sources* (relative to *root*) that import a name *is_owner* accepts."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                is_owner(name) for name in imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found
