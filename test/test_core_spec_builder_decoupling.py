"""GATE: Kiro Crew core does not depend on the Spec Builder (``spec-builder``) app's code.

Spec Builder is to leave this repository whole. That is only possible while nothing
outside ``src/kiro_crew/apps/builtins/`` imports it or names it. What the app keeps
on the dashboard state it creates there itself: the outbox's process-local claim
sets are not declared by ``DashboardState``. The scans are the shared ones in
``test/core_app_independence.py``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import spec_builder``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path
   built from ``"spec_builder"``, or a lookup keyed on the app id
   ``"spec-builder"``, reaches the app as surely as an import does. Docstrings
   are prose and are not read. The rows left are core's audit and lint of the
   app's files, which an app may not write for itself. Each is deleted in the PR
   that removes the app. A new one fails here, and so does a listed one that is
   gone.

The dashboard half is ``website/src/apps/spec-builder/specBuilderBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam
the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_spec_builder_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.spec_builder"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("spec_builder", "spec-builder")

#: (path relative to ``src/kiro_crew``, or to the repo root for a script, the
#: literal): the strings core keeps on purpose, each deleted with the file it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        # The security-posture rows that classify the app's backend modules as
        # the app's own HTTP surface rather than core egress.
        *(
            ("security_posture.py", f"apps/builtins/spec_builder/backend/{module}")
            for module in (
                "handlers.py",
                "parsers.py",
                "repository.py",
                "repository_discovery.py",
                "repository_git.py",
                "repository_sentinel.py",
                "runtime.py",
            )
        ),
        # The docs lint's runtime-data row for the decision record that
        # `security.md` cites under the gated `trust/` directory. It goes with that
        # paragraph when the app leaves.
        ("scripts/docs_lint.py", "trust/spec-builder-decisions.json"),
    }
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the Spec Builder app; add a generic seam the app fills instead:\n"
        + "\n".join(offenders)
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, KNOWN_NAME_REFERENCES)
    assert not unexpected, "a core string names the Spec Builder app:\n" + "\n".join(unexpected)
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


def test_the_dashboard_state_declares_no_spec_builder_claim():
    """The outbox claim sets are the app's, created on first use, never core's."""
    from kiro_crew.dashboard.state_parts import dashboard_state

    source = Path(dashboard_state.__file__).read_text(encoding="utf-8")
    assert "_spec_decision_deliveries" not in source


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        (
            "from kiro_crew.apps.builtins.spec_builder.backend.runtime import _dispatch_turn",
            "kiro_crew.dashboard.x",
            False,
        ),
        ("import kiro_crew.apps.builtins.spec_builder.backend.routes", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import spec_builder", "kiro_crew.x", False),
        ("from ..apps.builtins.spec_builder import backend", "kiro_crew.dashboard.y", False),
        ("from .builtins.spec_builder.backend import routes", "kiro_crew.apps.y", False),
        ("from .builtins import spec_builder", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.spec_builder.backend')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.spec_builder')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.spec_builder.backend import runtime",
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
        "import kiro_crew.apps.builtins.spec_builder_extras",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_allowlists_ids_or_urls():
    tree = ast.parse(
        '"""Mentions spec-builder in prose."""\n'
        "# a worker slot (Spec Builder's spec-builder, for one)\n"
        '_APPS = ("", "spec-builder")\n'
        'pkg = "kiro_crew.apps.builtins." + "spec_builder"\n'
        'url = f"/api/apps/spec-builder/{name}"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (3, "spec-builder"),
        (4, "spec_builder"),
        (5, "/api/apps/spec-builder/"),
    ]
