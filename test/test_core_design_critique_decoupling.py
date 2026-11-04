"""GATE: Kiro Crew core does not depend on the Design Critique app's code.

Design Critique is to leave this repository whole. That is only possible while
nothing outside ``src/kiro_crew/apps/builtins/`` imports it or names it. What the
core needs from the app it reads through a seam that is empty by default: the
worker slot mode the chat create paths accept comes from ``kiro_crew.apps.seams``,
where the app's own ``seams.py`` registers it.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import design_critique``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **No core string names the app.** A hard-coded allowlist entry
   ``"design-critique"``, or a dynamic import built from ``"design_critique"``,
   reaches the app as surely as an import does. Docstrings and comments are prose
   and are not read.

The scans are the shared ones in ``test/core_app_independence.py``. The
dashboard half is ``website/src/apps/design-critique/designCritiqueBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam
the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_design_critique_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.design_critique"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("design_critique", "design-critique")


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the design-critique app; add a generic seam the app fills instead:\n"
        + "\n".join(offenders)
    )


def test_no_core_string_names_the_app(core_trees):
    found, _allowed = independence.core_name_strings(core_trees, APP_NAMES)
    assert not found, "a core string names the design-critique app:\n" + "\n".join(found)


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.design_critique.backend.routes", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.design_critique import seams", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import design_critique", "kiro_crew.x", False),
        ("from ..apps.builtins.design_critique import seams", "kiro_crew.dashboard.y", False),
        ("from .builtins.design_critique.backend import routes", "kiro_crew.apps.y", False),
        ("from .builtins import design_critique", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.design_critique')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.design_critique.backend')", "kiro_crew.x", False),
    ],
)
def test_the_scan_catches_every_import_form(source, importer, is_package):
    assert independence.app_imports(ast.parse(source), APP_PACKAGE, importer, is_package)


@pytest.mark.parametrize(
    "source",
    [
        "from kiro_crew.apps import seams",
        "from kiro_crew.apps.builtins import BUILTIN_NAMES",
        "import kiro_crew.apps.builtins.design_tweak",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_allowlists_ids_or_urls():
    tree = ast.parse(
        '"""Mentions design-critique in prose."""\n'
        "# an app-worker mode (Design Critique's design-critique, for one)\n"
        '_MODES = ("", "design-critique")\n'
        'pkg = "kiro_crew.apps.builtins." + "design_critique"\n'
        'url = f"/api/apps/design-critique/{app}"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (3, "design-critique"),
        (4, "design_critique"),
        (5, "/api/apps/design-critique/"),
    ]
