"""Kiro Crew core imports no code from the built-in apps that are leaving the repository.

Dev Fleet and PPTX Maker are to move out of this repository intact, so no module
outside ``src/kiro_crew/apps/builtins/`` may import either package — by an ``import``
statement, a ``from`` import (absolute or relative), or an ``importlib.import_module`` /
``__import__`` call spelled with a literal. What the core needs from such an app it
reads through an app-keyed seam that is empty by default
(``kiro_crew.apps.builtin_host_grants``, ``bridges_agents._placeholder_values``), and a
capability the core itself offers lives in the core, where an app that needs it too
imports it (``kiro_crew.soffice_discovery``).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_PACKAGE = _SRC / "kiro_crew"
_BUILTINS = _PACKAGE / "apps" / "builtins"

#: The app packages core must not import, as dotted module names.
_LEAVING = ("kiro_crew.apps.builtins.dev_fleet", "kiro_crew.apps.builtins.pptx_maker")

_IMPORT_CALLS = frozenset({"import_module", "__import__"})


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
    as ``X.y`` so a ``from kiro_crew.apps.builtins import dev_fleet`` is caught."""
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
    leaves = tuple(app.rpartition(".")[2] for app in _LEAVING)
    for path in paths:
        text = path.read_text(encoding="utf-8")
        # Every spelling the scan reads names the package's last segment literally.
        if not any(leaf in text for leaf in leaves):
            continue
        # The package a relative import resolves against: the module's parent, or the
        # package itself for an ``__init__``.
        package = _dotted(path).rpartition(".")[0]
        for line, target in _targets(ast.parse(text, filename=str(path)), package):
            if any(target == app or target.startswith(app + ".") for app in _LEAVING):
                out.setdefault(f"{path.relative_to(_SRC)}:{line}", target)
    return [f"{where} imports {target}" for where, target in out.items()]


def test_no_core_module_imports_a_leaving_app() -> None:
    assert _offences(_core_modules()) == []


@pytest.mark.parametrize(
    ("relpath", "source"),
    [
        ("kiro_crew/a.py", "import kiro_crew.apps.builtins.dev_fleet.live\n"),
        ("kiro_crew/a.py", "from kiro_crew.apps.builtins import pptx_maker\n"),
        ("kiro_crew/a.py", "from kiro_crew.apps.builtins.pptx_maker.backend import engine\n"),
        ("kiro_crew/apps/a.py", "from .builtins.dev_fleet import gateway_routes\n"),
        ("kiro_crew/apps/a.py", "from .builtins import dev_fleet\n"),
        (
            "kiro_crew/a.py",
            "import importlib\nimportlib.import_module('kiro_crew.apps.builtins.dev_fleet')\n",
        ),
        ("kiro_crew/a.py", "__import__('kiro_crew.apps.builtins.pptx_maker.backend')\n"),
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
        "import kiro_crew.apps.builtins.dev_fleetish\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_SRC", root)
    assert _offences([target]) == []
