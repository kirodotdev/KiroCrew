"""GATE: Kiro Crew core does not depend on the Files (``file-explorer``) app's code.

The Files app is to leave this repository whole. That is only possible while
nothing outside ``src/kiro_crew/apps/builtins/`` imports it or names it. What the
core offers the app (the binary-extension set, the path-safety helpers, the
sandbox) lives in the core, and the app imports it from there; its parity pins
run in the app's own ``tests/``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import file_explorer``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **No core string names the app.** A path built from ``"file_explorer"``, or a
   lookup keyed on the app id ``"file-explorer"``, reaches the app as surely as an
   import does. Docstrings are prose and are not read.

The scans are the shared ones in ``test/core_app_independence.py``. The
dashboard half is ``website/src/apps/file-explorer/fileExplorerBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam
the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_file_explorer_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.file_explorer"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("file_explorer", "file-explorer")


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the file-explorer app; add a generic seam the app fills instead:\n"
        + "\n".join(offenders)
    )


def test_no_core_string_names_the_app(core_trees):
    found, _allowed = independence.core_name_strings(core_trees, APP_NAMES)
    assert not found, "a core string names the file-explorer app:\n" + "\n".join(found)


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.file_explorer.server", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.file_explorer import server", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import file_explorer", "kiro_crew.x", False),
        ("from ..apps.builtins.file_explorer import server", "kiro_crew.dashboard.y", False),
        ("from .builtins.file_explorer import server", "kiro_crew.apps.y", False),
        ("from .builtins import file_explorer", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.file_explorer.server')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.file_explorer')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.file_explorer.server import BINARY_EXTS",
            "kiro_crew.x",
            False,
        ),
    ],
)
def test_the_scan_catches_every_import_form(source, importer, is_package):
    assert independence.app_imports(ast.parse(source), APP_PACKAGE, importer, is_package)


@pytest.mark.parametrize(
    "source",
    [
        "from kiro_crew.dashboard.handlers import files",
        "from kiro_crew.apps.builtins import BUILTIN_NAMES",
        "import kiro_crew.apps.builtins.file_explorer_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_or_ids():
    tree = ast.parse(
        '"""Mentions file_explorer in prose."""\n'
        'SERVER = ("apps", "builtins", "file_explorer", "server.py")\n'
        'APP = "file-explorer"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (2, "file_explorer"),
        (3, "file-explorer"),
    ]
