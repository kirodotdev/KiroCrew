"""GATE: Kiro Crew core does not depend on the PPTX Maker (``pptx-maker``) app's code.

PPTX Maker is to leave this repository whole. That is only possible while nothing
outside ``src/kiro_crew/apps/builtins/`` imports it or names it. What the core
needs from the app it reads through a generic, app-keyed seam the app fills; what
the app needs from the core it imports from the core. The app's pins of core
parity (the shared credential spelling, the one-scope spawn layer, the Windows
containment fold, its agents' welcome hints) run in the app's own ``tests/``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import pptx_maker``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path
   built from ``"pptx_maker"``, or a lookup keyed on the app id ``"pptx-maker"``,
   reaches the app as surely as an import does. Docstrings are prose and are not
   read. The rows left are listed in ``KNOWN_NAME_REFERENCES`` with the reason
   each stays; a new one fails here, and so does a listed one that is gone.

``PENDING_IMPORTS`` and ``PENDING_NAME_REFERENCES`` are the agent-template
placeholder lookup and the slide-preview ``soffice`` lookup, which the open
dev-fleet decoupling change moves behind an app-keyed seam and into the core. They
are tolerated, never required, so this gate holds whichever of the two changes
lands first; once that change is on main its own import gate refuses the imports,
and both sets are deleted.

The scans are the shared ones in ``test/core_app_independence.py``. The dashboard
half is ``website/src/apps/pptx-maker/pptxMakerBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam
the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_pptx_maker_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.pptx_maker"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("pptx_maker", "pptx-maker")

#: (path relative to ``src/kiro_crew``, the literal): the core strings that still
#: name the app, each with why it is not a seam. Each row is deleted together with
#: the file or the skill it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        # The app's chat skill ships in ``builtin_skills/pptx-maker/`` like every
        # other packaged skill, so the closed skill-id list names it;
        # ``test_feature_usage`` pins that list equal to the tree.
        ("metrics/feature_usage.py", "pptx-maker"),
        # The security-posture tables classify named files of the package, built-in
        # apps included: the core's audit of the app's code, not a reach into it.
        ("security_posture.py", "apps/builtins/pptx_maker/backend/paths.py"),
        ("security_posture.py", "apps/builtins/pptx_maker/backend/decks.py"),
        ("security_posture.py", "apps/builtins/pptx_maker/backend/routes.py"),
    }
)

#: (path relative to the repository root, imported module) rows tolerated until
#: the dev-fleet decoupling change removes them; see the module docstring.
PENDING_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("src/kiro_crew/apps/bridges_agents.py", "kiro_crew.apps.builtins.pptx_maker.backend"),
        (
            "src/kiro_crew/dashboard/handlers/office_slides.py",
            "kiro_crew.apps.builtins.pptx_maker.backend",
        ),
    }
)

#: The literal rows tolerated for the same reason, keyed like ``KNOWN_NAME_REFERENCES``.
PENDING_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {("apps/bridges_agents.py", "pptx-maker")}
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def _is_pending_import(offender: str) -> bool:
    """Whether a ``"<path>:<line>: <module>"`` offender is a ``PENDING_IMPORTS`` row."""
    path, _, rest = offender.partition(":")
    return (path, rest.partition(": ")[2]) in PENDING_IMPORTS


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = [
        o
        for o in independence.core_app_imports(core_trees, APP_PACKAGE)
        if not _is_pending_import(o)
    ]
    assert (
        not offenders
    ), "core imports the pptx-maker app; add a generic seam the app fills instead:\n" + "\n".join(
        offenders
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(
        core_trees, APP_NAMES, KNOWN_NAME_REFERENCES | PENDING_NAME_REFERENCES
    )
    assert (
        not unexpected
    ), "a core string names the pptx-maker app; reach it through a seam:\n" + "\n".join(unexpected)
    assert found >= KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


def test_a_pending_import_is_tolerated_only_where_it_is_listed():
    """The pending filter spares its exact rows and nothing next to them."""
    backend = "kiro_crew.apps.builtins.pptx_maker.backend"
    assert _is_pending_import(f"src/kiro_crew/apps/bridges_agents.py:751: {backend}")
    assert not _is_pending_import(f"src/kiro_crew/apps/bridges.py:9: {backend}")
    assert not _is_pending_import(f"src/kiro_crew/apps/bridges_agents.py:9: {backend}.paths")


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.pptx_maker.backend.routes", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.pptx_maker.backend import engine", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import pptx_maker", "kiro_crew.x", False),
        ("from ..apps.builtins.pptx_maker.backend import paths", "kiro_crew.dashboard.y", False),
        ("from .builtins.pptx_maker import agent_placeholders", "kiro_crew.apps.y", False),
        ("from .builtins import pptx_maker", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.pptx_maker.backend.engine')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.pptx_maker')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.pptx_maker.backend import decks",
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
        "from kiro_crew.apps import bridges_agents",
        "from kiro_crew.apps.builtins import BUILTIN_NAMES",
        "import kiro_crew.apps.builtins.pptx_maker_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_or_ids():
    tree = ast.parse(
        '"""Mentions pptx_maker in prose."""\n'
        'ENGINE = ("apps", "builtins", "pptx_maker", "backend")\n'
        'APP = "pptx-maker"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (2, "pptx_maker"),
        (3, "pptx-maker"),
    ]
