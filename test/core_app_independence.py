"""The scanner behind every "core does not depend on built-in app X" gate.

A built-in app can leave this repository whole only while nothing outside
``src/kiro_crew/apps/builtins/`` reaches it. Each app's gate
(``test_core_<app>_decoupling.py``) calls the two scans here with its own package
and names, so a later app adds a gate of a few lines, never another scanner.

- :func:`core_app_imports` reports every core module or script that imports the
  app's package: ``import``, ``from ... import`` (relative ones resolved), ``from
  kiro_crew.apps.builtins import <app>``, and ``importlib.import_module`` /
  ``__import__`` with a literal name.
- :func:`core_name_strings` reports every non-docstring string constant that
  names the app, except the reasoned rows a gate passes as ``allowed``.

"Core" is every module under ``src/kiro_crew`` outside the skipped trees below,
plus every script under ``scripts/``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kiro_crew"

#: Trees under ``src/kiro_crew`` no gate reads: the builtin apps themselves, and
#: generated or vendored code.
_SKIPPED = ("apps/builtins/", "static/", "_vendor/")


def _module_name(path: Path) -> str:
    """The dotted module name of *path* under ``src/``, for resolving relative imports."""
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_app(name: str, app_package: str) -> bool:
    return name == app_package or name.startswith(app_package + ".")


def _resolve_relative(module: str | None, level: int, importer: str, is_package: bool) -> str:
    package = importer.split(".") if is_package else importer.split(".")[:-1]
    base = package[: len(package) - (level - 1)] if level > 1 else package
    return ".".join([*base, module] if module else base)


def app_imports(
    tree: ast.AST, app_package: str, importer: str, is_package: bool
) -> list[tuple[int, str]]:
    """``(line, imported name)`` for every way *tree* reaches *app_package*."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits.extend((node.lineno, a.name) for a in node.names if _is_app(a.name, app_package))
        elif isinstance(node, ast.ImportFrom):
            base = (
                _resolve_relative(node.module, node.level, importer, is_package)
                if node.level
                else node.module or ""
            )
            if _is_app(base, app_package):
                hits.append((node.lineno, base))
            else:
                hits.extend(
                    (node.lineno, f"{base}.{a.name}")
                    for a in node.names
                    if _is_app(f"{base}.{a.name}", app_package)
                )
        elif isinstance(node, ast.Call):
            func = node.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else ""
            )
            if called in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if _is_app(arg.value, app_package):
                        hits.append((node.lineno, arg.value))
    return hits


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                nodes.add(id(body[0].value))
    return nodes


def name_strings(tree: ast.AST, names: Iterable[str]) -> list[tuple[int, str]]:
    """``(line, literal)`` for every non-docstring string constant holding one of *names*."""
    names = tuple(names)
    docs = _docstring_nodes(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(name in node.value for name in names)
        and id(node) not in docs
    ]


def _core_files() -> list[Path]:
    files = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if not rel.startswith(_SKIPPED):
            files.append(path)
    return [*files, *sorted((ROOT / "scripts").rglob("*.py"))]


def core_trees(names: Iterable[str]) -> dict[Path, ast.AST]:
    """The parsed core files whose text holds one of *names*.

    Every form both scans catch spells the app's package directory name in the
    source text, so a file holding none of *names* cannot hold a hit and is not
    parsed. *names* must therefore include that directory name.
    """
    names = tuple(names)
    trees = {}
    for path in _core_files():
        text = path.read_text(encoding="utf-8")
        if any(name in text for name in names):
            trees[path] = ast.parse(text, filename=str(path))
    return trees


def _label(path: Path) -> str:
    """*path* relative to ``src/kiro_crew`` for a core module, to the repo root otherwise."""
    if path.is_relative_to(SRC):
        return path.relative_to(SRC).as_posix()
    return path.relative_to(ROOT).as_posix()


def core_app_imports(trees: Mapping[Path, ast.AST], app_package: str) -> list[str]:
    """``"<path>:<line>: <name>"`` for every core import of *app_package* in *trees*."""
    offenders = []
    for path, tree in trees.items():
        if path.is_relative_to(SRC):
            importer, is_package = _module_name(path), path.name == "__init__.py"
        else:
            importer, is_package = path.stem, False
        offenders.extend(
            f"{path.relative_to(ROOT).as_posix()}:{line}: {name}"
            for line, name in app_imports(tree, app_package, importer, is_package)
        )
    return offenders


def core_name_strings(
    trees: Mapping[Path, ast.AST],
    names: Iterable[str],
    allowed: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[list[str], set[tuple[str, str]]]:
    """The core strings naming the app: ``(unexpected, allowed rows found)``.

    *allowed* holds ``(path relative to src/kiro_crew, literal)`` rows a gate keeps
    on purpose, each with its reason at the gate. A row matched is returned in the
    second item, so the gate can fail on one that is gone; every other hit is an
    ``"<path>:<line>: <literal>"`` line in the first.
    """
    names = tuple(names)
    unexpected = []
    found = set()
    for path, tree in trees.items():
        label = _label(path)
        for line, literal in name_strings(tree, names):
            if (label, literal) in allowed:
                found.add((label, literal))
            else:
                unexpected.append(f"{label}:{line}: {literal!r}")
    return unexpected, found
