"""GATE: Kiro Crew core imports no code from the Dev Fleet and PPTX Maker apps.

Both apps are to move out of this repository intact, so no module under
``src/kiro_crew`` outside ``apps/builtins/``, and no script under ``scripts/``, may
import either package: ``import``, ``from ... import`` (relative ones resolved),
``from kiro_crew.apps.builtins import <app>``, ``importlib.import_module`` and
``__import__`` with a literal name are all in scope, and the probes below fail if any
of those forms stops being caught. The scans are the shared ones in
``test/core_app_independence.py``.

What the core needs from such an app it reads through an app-keyed seam that is empty
by default (``kiro_crew.apps.builtin_host_grants``,
``bridges_agents._placeholder_values``), and a capability the core itself offers lives
in the core, where an app that needs it too imports it
(``kiro_crew.soffice_discovery``). If this goes red, invert the dependency that way.
Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_dev_fleet_pptx_maker_decoupling")

#: ``(app package, package directory name)`` for each app core must not import.
APPS = (
    ("kiro_crew.apps.builtins.dev_fleet", "dev_fleet"),
    ("kiro_crew.apps.builtins.pptx_maker", "pptx_maker"),
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention either package name at all."""
    return independence.core_trees([name for _package, name in APPS])


@pytest.mark.parametrize("app_package", [package for package, _name in APPS])
def test_no_core_module_or_script_imports_the_app(core_trees, app_package):
    offenders = independence.core_app_imports(core_trees, app_package)
    assert (
        not offenders
    ), f"core imports {app_package}; read it through an app-keyed seam instead:\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize(
    ("source", "app_package", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.dev_fleet.live", "dev_fleet", "kiro_crew.a", False),
        ("from kiro_crew.apps.builtins import pptx_maker", "pptx_maker", "kiro_crew.a", False),
        (
            "from kiro_crew.apps.builtins.pptx_maker.backend import engine",
            "pptx_maker",
            "kiro_crew.a",
            False,
        ),
        ("from .builtins.dev_fleet import gateway_routes", "dev_fleet", "kiro_crew.apps.a", False),
        ("from .builtins import dev_fleet", "dev_fleet", "kiro_crew.apps", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.apps.builtins.dev_fleet')",
            "dev_fleet",
            "kiro_crew.a",
            False,
        ),
        (
            "__import__('kiro_crew.apps.builtins.pptx_maker.backend')",
            "pptx_maker",
            "kiro_crew.a",
            False,
        ),
    ],
)
def test_the_scan_catches_every_import_form(source, app_package, importer, is_package):
    package = f"kiro_crew.apps.builtins.{app_package}"
    assert independence.app_imports(ast.parse(source), package, importer, is_package)


@pytest.mark.parametrize("app_package", [package for package, _name in APPS])
def test_the_scan_leaves_other_imports_alone(app_package):
    tree = ast.parse(
        "from kiro_crew.apps.builtins import BUILTIN_NAMES\n"
        "import kiro_crew.apps.builtins.dev_fleetish\n"
        "import kiro_crew.apps.builtins.pptx_maker_other\n"
    )
    assert not independence.app_imports(tree, app_package, "kiro_crew.a", False)
