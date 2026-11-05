"""Checks the composition-contract test files share, written once.

Each check lives here as a mixin. A test class in ``test_*composition_contract*.py``
takes the mixin as a base, so the node id stays ``<file>::<Class>::<test>``, the class
keeps its own setup, and pytest resolves the test's fixtures from that file. A check
that reads the file's own constants or helpers does so through :func:`_file_module`.
"""

from __future__ import annotations

import ast
import sys
from types import ModuleType


def _file_module(test: object) -> ModuleType:
    """The test file the class running this check is defined in."""
    return sys.modules[type(test).__module__]


class TheFrozenInventoryIsNotEmpty:
    def test_the_frozen_inventory_is_not_empty(self) -> None:
        # An emptied list would make the case below pass while checking nothing.
        here = _file_module(self)
        assert len(here.FROZEN_NAMES) > 200


class TheFacadeHelperIsOnlyEverCalledInPlace:
    def test_the_facade_helper_is_only_ever_called_in_place(self) -> None:
        # An alias (``f = _facade``) or a stored module would hide a sixth call site
        # from the reader above, so every reference to ``_facade`` in an owner is its
        # import or the callee of a call.
        here = _file_module(self)
        for part in here.PARTS:
            tree = ast.parse(here._source(part))
            callees = {
                id(node.func)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            stray = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id == "_facade" and id(node) not in callees
            ]
            assert stray == [], f"{part.__name__} refers to _facade at lines {stray}"


class ThePackageHoldsExactlyTheComposedModules:
    def test_the_package_holds_exactly_the_composed_modules(self) -> None:
        here = _file_module(self)
        assert set(here._owner_sources()) == here.OWNER_MODULES


class ThePackageInitImportsNothing:
    def test_the_package_init_imports_nothing(self) -> None:
        here = _file_module(self)
        tree = ast.parse((here.OWNER_DIR / "__init__.py").read_text(encoding="utf-8"))
        assert [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))] == []


class NoOwnerAnchorsAPathOnItsOwnFile:
    def test_no_owner_anchors_a_path_on_its_own_file(self) -> None:
        here = _file_module(self)
        for stem, source in here._owner_sources().items():
            names = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)]
            assert not [n for n in names if n.id == "__file__"], stem


class ThePackageHoldsExactlyTheComposedParts:
    def test_the_package_holds_exactly_the_composed_parts(self) -> None:
        here = _file_module(self)
        assert {path.stem for path in here.PARTS_DIR.glob("*.py")} == {*here.PARTS, "__init__"}


class ThePackageInitHoldsOnlyItsDocstring:
    def test_the_package_init_holds_only_its_docstring(self) -> None:
        here = _file_module(self)
        body = here._tree(here.PARTS_DIR / "__init__.py").body
        assert len(body) == 1 and isinstance(body[0], ast.Expr)
        assert isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)


class TheRuntimePackageHoldsExactlyTheComposedModules:
    def test_the_runtime_package_holds_exactly_the_composed_modules(self) -> None:
        here = _file_module(self)
        assert set(here._runtime_sources()) == here.RUNTIME_MODULES


class ThePackageShipsWithTheWheel:
    def test_the_package_ships_with_the_wheel(self) -> None:
        """``packages = find:`` (not ``find_namespace:``) only ships a directory that
        carries an ``__init__.py``; an editable install would import it anyway."""
        here = _file_module(self)
        import configparser

        config = configparser.ConfigParser()
        config.read(here.FACADE_PATH.parents[2] / "setup.cfg", encoding="utf-8")
        assert config.get("options", "packages").strip() == "find:"
        assert config.get("options.packages.find", "where").strip() == "src"
        assert (here.RUNTIME_DIR / "__init__.py").is_file()


class NoRuntimeModuleReadsASeamAsABareGlobal:
    def test_no_runtime_module_reads_a_seam_as_a_bare_global(self) -> None:
        here = _file_module(self)
        offenders = {
            stem: hits
            for stem, source in here._runtime_sources().items()
            if (hits := self._bare_seam_loads(source))
        }
        assert offenders == {}


class NoRuntimeModuleDefinesAnAsyncFunction:
    def test_no_runtime_module_defines_an_async_function(self) -> None:
        here = _file_module(self)
        for stem, source in here._runtime_sources().items():
            assert not [
                n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef)
            ], stem
