"""Every adopted app's wheel copy still finds each core name it imports.

An adopted host runs the app's own repository at its pin, against this core, and the
wheel copy is that repository's twin (PR Hygiene holds them equal). So a ``kiro_crew``
name the copy imports that the core lacks is a name the adopted app cannot import
either, and the app breaks on that host.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import kiro_crew.apps.builtins as builtins_pkg
from kiro_crew.apps.builtin_adoption_pins import adoption_pin_names

_BUILTINS = Path(builtins_pkg.__file__).resolve().parent


def _core_imports(root: Path) -> list[tuple[str, str | None, str]]:
    """``(module, name, site)`` for every absolute ``kiro_crew`` import under *root*."""
    found: list[tuple[str, str | None, str]] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "kiro_crew" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            site = f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] == "kiro_crew":
                    found += [(node.module, alias.name, site) for alias in node.names]
            elif isinstance(node, ast.Import):
                found += [
                    (alias.name, None, site)
                    for alias in node.names
                    if alias.name.split(".")[0] == "kiro_crew"
                ]
    return found


def _resolves(module: str, name: str | None) -> bool:
    try:
        owner = importlib.import_module(module)
    except ImportError:
        return False
    if name is None or name == "*" or hasattr(owner, name):
        return True
    try:
        importlib.import_module(f"{module}.{name}")
    except ImportError:
        return False
    return True


@pytest.mark.parametrize("app", sorted(adoption_pin_names()))
def test_an_adopted_app_finds_every_core_name_it_imports(app: str) -> None:
    root = _BUILTINS / app.replace("-", "_")
    assert root.is_dir(), f"{app} has a pin but no wheel copy under {_BUILTINS}"
    imports = _core_imports(root)
    assert imports, f"{app} imports nothing from the core, so this check reads nothing"
    missing = [
        f"{site} {module}:{name}" for module, name, site in imports if not _resolves(module, name)
    ]
    assert missing == [], missing


def test_a_removed_core_name_is_reported(tmp_path: Path) -> None:
    """The check names an import the core cannot answer, so it cannot pass by reading nothing."""
    (tmp_path / "backend.py").write_text(
        "from kiro_crew.security import redact\n"
        "from kiro_crew.security.exfil import NO_SUCH_CORE_NAME\n",
        encoding="utf-8",
    )
    imports = _core_imports(tmp_path)
    assert [(module, name) for module, name, _ in imports] == [
        ("kiro_crew.security", "redact"),
        ("kiro_crew.security.exfil", "NO_SUCH_CORE_NAME"),
    ]
    assert [_resolves(module, name) for module, name, _ in imports] == [True, False]
