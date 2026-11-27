"""GATE: Kiro Crew core does not depend on the Create Folders From Project app.

The app (``project-scaffolder``) ships no wheel copy: it runs from its own repository
at the commit its adoption pin names, so nothing in core may import it or name it.
Its page still calls two host routes, ``POST /api/project-scaffold/scan`` and
``/create``; they answer while an enabled app holds an approved ``permissions.api``
entry inside ``/api/project-scaffold`` (the app-agnostic seam
``kiro_crew.apps.core_route_grants``), so core never asks about this app by name.
The adoption pin row is data, not a core module, and is not read here.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed, and the probes below fail if any import form stops
   being caught.
2. **No core string names the app.** A lookup keyed on the app id
   ``"project-scaffolder"``, or a path built from ``"project_scaffolder"``, reaches
   the app as surely as an import does. Docstrings are prose and are not read. The
   route namespace ``/api/project-scaffold`` is not the app's name and is not caught.

The scans are the shared ones in ``test/core_app_independence.py``.

If this goes red because core reached for the app, add a generic, app-keyed seam the
app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_project_scaffolder_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.project_scaffolder"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("project_scaffolder", "project-scaffolder")


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the project-scaffolder app; add a generic seam the app fills "
        "instead:\n" + "\n".join(offenders)
    )


def test_no_core_string_names_the_app(core_trees):
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES)
    assert not unexpected, "a core string names the project-scaffolder app:\n" + "\n".join(
        unexpected
    )
    assert found == set()


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.project_scaffolder", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.project_scaffolder import tests", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import project_scaffolder", "kiro_crew.x", False),
        ("from ..apps.builtins.project_scaffolder import tests", "kiro_crew.dashboard.y", False),
        ("from .builtins import project_scaffolder", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.project_scaffolder')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.project_scaffolder')", "kiro_crew.x", False),
    ],
)
def test_the_scan_catches_every_import_form(source, importer, is_package):
    assert independence.app_imports(ast.parse(source), APP_PACKAGE, importer, is_package)


def test_the_string_scan_catches_the_id_but_not_the_route_namespace():
    tree = ast.parse(
        '"""Mentions project-scaffolder in prose."""\n'
        'APP = "project-scaffolder"\n'
        'PACKAGE = ("apps", "builtins", "project_scaffolder")\n'
        'NAMESPACE = "/api/project-scaffold"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (2, "project-scaffolder"),
        (3, "project_scaffolder"),
    ]
