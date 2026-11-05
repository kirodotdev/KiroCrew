"""Kiro Crew core imports no Papyrus code and names the app nowhere it could act on it.

Papyrus is to move out of this repository intact, so no module outside
``src/kiro_crew/apps/builtins/`` may import its package -- by an ``import`` statement,
a ``from`` import (absolute or relative), or an ``importlib.import_module`` /
``__import__`` call spelled with a literal. The core reaches it only the way it
reaches every built-in: by name through ``BUILTIN_NAMES``.

The second half is a ratchet on the app's NAME in core code. Every string literal
outside a docstring that mentions it is listed below with the reason it stays, so a
new one -- a hard-coded route, an app-id branch, an allowlist row -- fails here
instead of quietly re-coupling the core to the app.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_PACKAGE = _SRC / "kiro_crew"
_BUILTINS = _PACKAGE / "apps" / "builtins"

_APP = "kiro_crew.apps.builtins.papyrus"
_LEAF = _APP.rpartition(".")[2]

_IMPORT_CALLS = frozenset({"import_module", "__import__"})

#: Every core string literal that names the app, and why it stays.
_NAMED_LITERALS: dict[tuple[str, str], str] = {
    # The five Papyrus skills ship in ``builtin_skills/`` like every other packaged
    # skill, so the closed skill-id list names them; ``test_feature_usage`` pins the
    # list equal to that tree.
    ("kiro_crew/metrics/feature_usage.py", "papyrus-diagnose-compilation"): "shipped skill",
    ("kiro_crew/metrics/feature_usage.py", "papyrus-latex-comments"): "shipped skill",
    ("kiro_crew/metrics/feature_usage.py", "papyrus-latex-suggestions"): "shipped skill",
    ("kiro_crew/metrics/feature_usage.py", "papyrus-make-fluent"): "shipped skill",
    ("kiro_crew/metrics/feature_usage.py", "papyrus-writing"): "shipped skill",
    # The redaction-posture classification names every module in the package that
    # calls the redactor, built-in apps included; a row leaves with its module.
    ("kiro_crew/security_posture.py", "apps/builtins/papyrus/backend/routes.py"): "posture row",
    # Setup's sandbox prompt names the app as an example of what a refusal disables.
    (
        "kiro_crew/cli_setup.py",
        "  Papyrus compiler on this host — they will report a sandbox error.",
    ): "setup copy",
    (
        "kiro_crew/cli_setup.py",
        "  subprocesses at all — MCP servers, Dev Fleet and the Papyrus",
    ): "setup copy",
}


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(_SRC).with_suffix("").parts)


def _resolve(module: str | None, level: int, package: str) -> str:
    if not level:
        return module or ""
    parts = package.split(".")
    base = parts[: len(parts) - level + 1]
    return ".".join([*base, module] if module else base)


def _targets(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    """Every ``(line, dotted target)`` the module imports, including ``from X import y``
    as ``X.y`` so a ``from kiro_crew.apps.builtins import papyrus`` is caught."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node.module, node.level, package)
            found.append((node.lineno, base))
            found.extend((node.lineno, f"{base}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            first = node.args[0]
            if (
                name in _IMPORT_CALLS
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                found.append((node.lineno, first.value))
    return found


def _core_modules() -> list[Path]:
    return sorted(p for p in _PACKAGE.rglob("*.py") if _BUILTINS not in p.parents)


def _offences(paths: list[Path]) -> list[str]:
    """One ``path:line`` per offending import statement or call."""
    out: dict[str, str] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        # Every spelling the scan reads names the package's last segment literally.
        if _LEAF not in text:
            continue
        # The package a relative import resolves against: the module's parent, or the
        # package itself for an ``__init__``.
        package = _dotted(path).rpartition(".")[0]
        for line, target in _targets(ast.parse(text, filename=str(path)), package):
            if target == _APP or target.startswith(_APP + "."):
                out.setdefault(f"{path.relative_to(_SRC).as_posix()}:{line}", target)
    return [f"{where} imports {target}" for where, target in out.items()]


def _named_literals(paths: list[Path]) -> set[tuple[str, str]]:
    """Every ``(module, literal)`` outside a docstring whose text names the app."""
    found: set[tuple[str, str]] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if _LEAF not in text.lower():
            continue
        tree = ast.parse(text, filename=str(path))
        bare = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in bare
                and _LEAF in node.value.lower()
            ):
                found.add((rel, node.value))
    return found


def test_no_core_module_imports_papyrus() -> None:
    assert _offences(_core_modules()) == []


def test_core_names_papyrus_only_where_listed() -> None:
    assert _named_literals(_core_modules()) == set(_NAMED_LITERALS)


@pytest.mark.parametrize(
    ("relpath", "source"),
    [
        ("kiro_crew/a.py", "import kiro_crew.apps.builtins.papyrus.backend.routes\n"),
        ("kiro_crew/a.py", "from kiro_crew.apps.builtins import papyrus\n"),
        ("kiro_crew/a.py", "from kiro_crew.apps.builtins.papyrus.backend import latex\n"),
        ("kiro_crew/apps/a.py", "from .builtins.papyrus.backend import store\n"),
        ("kiro_crew/apps/a.py", "from .builtins import papyrus\n"),
        (
            "kiro_crew/a.py",
            "import importlib\nimportlib.import_module('kiro_crew.apps.builtins.papyrus')\n",
        ),
        ("kiro_crew/a.py", "__import__('kiro_crew.apps.builtins.papyrus.backend')\n"),
    ],
)
def test_every_import_spelling_is_caught(tmp_path, monkeypatch, relpath, source) -> None:
    """The scan is only as good as the spellings it reads, so each one is proven."""
    root = tmp_path / "src"
    target = root / relpath
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_SRC", root)
    assert len(_offences([target])) == 1


def test_an_unrelated_builtin_import_is_not_flagged(tmp_path, monkeypatch) -> None:
    root = tmp_path / "src"
    target = root / "kiro_crew" / "a.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from kiro_crew.apps.builtins import BUILTIN_NAMES\n"
        "import kiro_crew.apps.builtins.papyrusish\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_SRC", root)
    assert _offences([target]) == []


def test_a_new_literal_naming_the_app_is_caught(tmp_path, monkeypatch) -> None:
    root = tmp_path / "src"
    target = root / "kiro_crew" / "a.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        '"""Papyrus in a docstring is prose."""\n'
        "# papyrus in a comment is prose too\n"
        "ROUTE = '/api/apps/papyrus/projects'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_SRC", root)
    assert _named_literals([target]) == {("kiro_crew/a.py", "/api/apps/papyrus/projects")}
