"""GATE: Kiro Crew core does not depend on the Research Lab (``auto-research``) app's code.

Research Lab is to leave this repository whole. That is only possible while nothing
outside ``src/kiro_crew/apps/builtins/`` imports it. A core feature that needs to know
something about the app's own slots asks a seam in :mod:`kiro_crew.apps.seams` (the
autonudge stop asks ``stopped_loop_tombstone``), and the app answers from its own
``seams.py``. The scans are the shared ones in
``test/core_app_independence.py``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import auto_research``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path
   built from ``"auto_research"``, or a lookup keyed on the app id
   ``"auto-research"``, reaches the app as surely as an import does. Docstrings are
   prose and are not read. The rows left are the security-posture table that
   classifies the app's files as the app's own surface (core's audit of the app's
   code, which an app may not write for itself). Each is deleted in the PR that
   removes the app. A new one fails here, and so does a listed one that is gone.

The dashboard half is ``website/src/apps/auto-research/researchLabBoundary.test.ts``.

If this goes red because core reached for the app, add a generic seam the app fills
instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_auto_research_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.auto_research"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("auto_research", "auto-research")

#: (path relative to ``src/kiro_crew``, the literal): the security-posture rows that
#: classify the app's backend modules as the app's own HTTP/SSE surface rather than
#: core egress. Each row is deleted together with the file it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    ("security_posture.py", f"apps/builtins/auto_research/{module}")
    for module in (
        "handlers.py",
        "campaign/agent_mode.py",
        "campaign/exploration.py",
        "campaign/lifecycle.py",
        "campaign/publication.py",
        "campaign/storage.py",
        "campaign/untrusted.py",
        "campaign/workflow_mode.py",
    )
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the Research Lab app; ask a seam in "
        "kiro_crew.apps.seams instead:\n" + "\n".join(offenders)
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, KNOWN_NAME_REFERENCES)
    assert not unexpected, "a core string names the Research Lab app:\n" + "\n".join(unexpected)
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        (
            "from kiro_crew.apps.builtins.auto_research.session_keys import is_owned_research_slot",
            "kiro_crew.dashboard.x",
            False,
        ),
        ("import kiro_crew.apps.builtins.auto_research.handlers", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import auto_research", "kiro_crew.x", False),
        ("from ..apps.builtins.auto_research import session_keys", "kiro_crew.dashboard.y", False),
        ("from .builtins.auto_research import session_keys", "kiro_crew.apps.y", False),
        ("from .builtins import auto_research", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.auto_research.session_keys')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.auto_research')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.auto_research.session_keys import (\n"
            "        is_owned_research_slot,\n"
            "    )",
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
        "from kiro_crew.apps import seams",
        "from kiro_crew.apps.builtins import BUILTIN_NAMES",
        "import kiro_crew.apps.builtins.auto_research_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_or_ids():
    tree = ast.parse(
        '"""Mentions auto_research in prose."""\n'
        'KEYS = ("apps", "builtins", "auto_research", "session_keys.py")\n'
        'APP = "auto-research"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (2, "auto_research"),
        (3, "auto-research"),
    ]
