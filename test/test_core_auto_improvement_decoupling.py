"""GATE: Kiro Crew core does not depend on the Auto-Improvement app's code.

Auto-Improvement is to leave this repository whole. That is only possible while
nothing outside ``src/kiro_crew/apps/builtins/`` imports it. The app already reaches
the core only through the core's public surfaces (the app imports core, never the
other way round), so no seam is needed for it today: what this gate keeps is that
direction. The scans are the shared ones in ``test/core_app_independence.py``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import auto_improvement``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path
   built from ``"auto_improvement"``, or a lookup keyed on the app id
   ``"auto-improvement"``, reaches the app as surely as an import does. Docstrings
   are prose and are not read. The rows left are core's own classification of the
   app's files and docs, which an app may not write for itself, and each is deleted
   in the PR that removes the app. A new one fails here, and so does a listed one
   that is gone.

The dashboard half is ``website/src/apps/auto-improvement/autoImprovementBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam in
:mod:`kiro_crew.apps.seams` the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_auto_improvement_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.auto_improvement"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("auto_improvement", "auto-improvement")

#: The security-posture redaction-audit rows that classify the app's modules as
#: source-side scrubbers or detectors rather than unredacted egress
#: (``security_posture.py``), and the gateway sink inventory that names the app's
#: registered output boundaries (``security_posture_sinks_gateway.py``). Security
#: classification of the app's code is core's audit, not the app's to carry.
_SECURITY_POSTURE_ROWS: frozenset[tuple[str, str]] = frozenset(
    ("security_posture.py", f"apps/builtins/auto_improvement/{module}")
    for module in (
        "spine/push_policy.py",
        "backend/deps.py",
        "backend/clone_setup.py",
        "profiles/github_repo/profile.py",
        "spine/driver_publish.py",
        "spine/gate.py",
    )
) | frozenset(
    ("security_posture_sinks_gateway.py", f"apps/builtins/auto_improvement/{module}")
    for module in (
        "spine/agent_runner.py",
        "spine/driver_commit.py",
        "backend/commit.py",
        "backend/mcp_server.py",
        "backend/pr_watchers.py",
        "backend/runner.py",
        "profiles/pr_recipe_base.py",
        "backend/routes.py",
    )
)

#: (path relative to ``src/kiro_crew``, or to the repository root for a script; the
#: literal): every core string still naming the app, each deleted with what it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = _SECURITY_POSTURE_ROWS | {
    # The docs linter's table of docs that describe a run against ANOTHER repository,
    # so a ``path::symbol`` coordinate in them is not a claim about this tree. The
    # row names the app's spec, which still lives in core's docs/ in this wave.
    ("scripts/docs_lint_fact_checks.py", "docs/system-specs/modules/auto-improvement.md"),
}


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the Auto-Improvement app; add a generic seam in "
        "kiro_crew.apps.seams the app fills instead:\n" + "\n".join(offenders)
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, KNOWN_NAME_REFERENCES)
    assert not unexpected, "a core string names the Auto-Improvement app:\n" + "\n".join(unexpected)
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        (
            "from kiro_crew.apps.builtins.auto_improvement.spine.git_safety import git_argv",
            "kiro_crew.x",
            False,
        ),
        ("import kiro_crew.apps.builtins.auto_improvement.backend.routes", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import auto_improvement", "kiro_crew.x", False),
        ("from ..apps.builtins.auto_improvement import spine", "kiro_crew.dashboard.y", False),
        ("from .builtins.auto_improvement.backend import store", "kiro_crew.apps.y", False),
        ("from .builtins import auto_improvement", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.auto_improvement.backend.crew')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.auto_improvement')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (\n"
            "        SessionAgentRunner,\n"
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
        "import kiro_crew.apps.builtins.auto_improvement_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_or_ids():
    tree = ast.parse(
        '"""Mentions auto_improvement in prose."""\n'
        "# the auto-improvement runner, in a comment\n"
        'KEYS = ("apps", "builtins", "auto_improvement", "spine")\n'
        'APP = "auto-improvement"\n'
        'url = f"/api/apps/auto-improvement/{route}"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (3, "auto_improvement"),
        (4, "auto-improvement"),
        (5, "/api/apps/auto-improvement/"),
    ]
