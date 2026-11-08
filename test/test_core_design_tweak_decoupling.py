"""GATE: Kiro Crew core does not depend on the Design Tweak (``design-tweak``) app's code.

The Design Tweak app is to leave this repository whole. That is only possible while
nothing outside ``src/kiro_crew/apps/builtins/`` imports it or names it. Its agent
tool, ``design_tweak_update_thread``, is declared in the app's own ``app.json``
(``agentTools``) and served by the app's own backend process; the core lists it and
forwards each call through the app-agnostic seam ``kiro_crew.apps.app_agent_tools``,
and no route of the app is admitted to internal-secret callers.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import design_tweak``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **No core string names the app**, except the pinned rows below. A path built from
   ``"design_tweak"``, or a lookup keyed on the app id ``"design-tweak"`` or on its
   tool's name, reaches the app as surely as an import does. Docstrings are prose and
   are not read.

The scans are the shared ones in ``test/core_app_independence.py``. The dashboard half
is ``website/src/apps/design-tweak/designTweakBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam the
app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_design_tweak_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.design_tweak"
#: The package directory name and the app id: every spelling that addresses the app.
#: The tool's name starts with the package name, so the first spelling covers it.
APP_NAMES = ("design_tweak", "design-tweak")

#: (path relative to ``src/kiro_crew``, the literal): the core strings that still name
#: the app, each with why it is not a seam. The security-posture redaction call-site
#: table classifies every shipped file that calls a redactor, the app's three backend
#: modules among them; it is the core's audit of code shipped in this tree, which an
#: app may not classify for itself, and it fails on a stale row. Each row is deleted
#: together with the file it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        ("security_posture.py", "apps/builtins/design_tweak/backend/http_api.py"),
        ("security_posture.py", "apps/builtins/design_tweak/backend/request_state.py"),
        ("security_posture.py", "apps/builtins/design_tweak/backend/server.py"),
    }
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the design-tweak app; add a generic seam the app fills instead:\n"
        + "\n".join(offenders)
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, KNOWN_NAME_REFERENCES)
    assert not unexpected, "a core string names the design-tweak app:\n" + "\n".join(unexpected)
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.design_tweak.agent_tools", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.design_tweak import agent_tools", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import design_tweak", "kiro_crew.x", False),
        ("from ..apps.builtins.design_tweak import agent_tools", "kiro_crew.mcp_tools.y", False),
        ("from .builtins.design_tweak import agent_tools", "kiro_crew.apps.y", False),
        ("from .builtins import design_tweak", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.design_tweak.agent_tools')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.design_tweak')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.design_tweak.agent_tools import AGENT_TOOLS",
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
        "from kiro_crew.apps import app_agent_tools",
        "from kiro_crew.apps.builtins import BUILTIN_NAMES",
        "import kiro_crew.apps.builtins.design_tweak_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_ids_or_tool_names():
    tree = ast.parse(
        '"""Mentions design_tweak in prose."""\n'
        'SERVER = ("apps", "builtins", "design_tweak", "backend")\n'
        'URL = "/apps/design-tweak/api/thread"\n'
        'TOOL = "design_tweak_update_thread"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (2, "design_tweak"),
        (3, "/apps/design-tweak/api/thread"),
        (4, "design_tweak_update_thread"),
    ]
