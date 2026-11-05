"""GATE: Kiro Crew core does not depend on the Code Review Sage app's code.

Code Review Sage is to leave this repository whole. That is only possible while
nothing outside ``src/kiro_crew/apps/builtins/`` imports it or names it. The app
already reaches core only through public surfaces it imports itself (the ACP worker
pool, ``github_runner``, the dashboard's pull-request panel), so no seam carries
anything for it today; a core feature that ever needs something from it asks a seam
in :mod:`kiro_crew.apps.seams`, and the app answers from its own ``seams.py``. The
scans are the shared ones in ``test/core_app_independence.py``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import code_review_sage``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path
   built from ``"code_review_sage"``, or a lookup keyed on the app id
   ``"code-review-sage"``, reaches the app as surely as an import does. Docstrings
   are prose and are not read. The rows left are listed in ``KNOWN_NAME_REFERENCES``
   with the reason each stays, and each is deleted in the PR that removes the app. A
   new one fails here, and so does a listed one that is gone.

The dashboard half is
``website/src/apps/code-review-sage/codeReviewSageBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam
the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_code_review_sage_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.code_review_sage"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("code_review_sage", "code-review-sage")

#: (path relative to ``src/kiro_crew``, or to the repository root for a script; the
#: literal): the core strings that still name the app, each with why it is not a seam.
#:
#: - ``security_posture.py``: the redaction rows that classify the app's modules as the
#:   app's own surface rather than core egress. They are core's audit of the app's
#:   code, which an app may not write for itself.
#: - ``scripts/docs_lint.py``: the doc-reference linter skips the app's test fixtures,
#:   which embed arbitrary diff paths as test data. It names a directory of the app's,
#:   so it goes when that directory does.
#:
#: Each row is deleted together with the file it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        *(
            ("security_posture.py", f"apps/builtins/code_review_sage/{module}")
            for module in (
                "sage_lib/learning.py",
                "sage_lib/pipeline.py",
                "sage_lib/report.py",
                "sage_lib/review_driver.py",
                "sage_lib/worker_response.py",
                "sage_lib/store.py",
                "sage_lib/discovery.py",
                "sage_lib/followup.py",
                "backend/routes.py",
                "backend/routes_parts/publishing.py",
            )
        ),
        ("scripts/docs_lint.py", "code_review_sage/tests/"),
    }
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the Code Review Sage app; ask a seam in "
        "kiro_crew.apps.seams instead:\n" + "\n".join(offenders)
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, KNOWN_NAME_REFERENCES)
    assert not unexpected, "a core string names the Code Review Sage app:\n" + "\n".join(unexpected)
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        (
            "from kiro_crew.apps.builtins.code_review_sage.sage_lib import review_driver",
            "kiro_crew.dashboard.x",
            False,
        ),
        ("import kiro_crew.apps.builtins.code_review_sage.backend.routes", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import code_review_sage", "kiro_crew.x", False),
        ("from ..apps.builtins.code_review_sage import sage_lib", "kiro_crew.dashboard.y", False),
        ("from .builtins.code_review_sage.backend import routes", "kiro_crew.apps.y", False),
        ("from .builtins import code_review_sage", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.code_review_sage.sage_lib.store')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.code_review_sage')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.code_review_sage.sage_lib import store",
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
        "import kiro_crew.apps.builtins.code_review_sage_other",
        "from kiro_crew.acp import worker_pool",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_ids_or_urls():
    tree = ast.parse(
        '"""Mentions code_review_sage in prose."""\n'
        "# a worker-pool client (code-review-sage, for one)\n"
        'KEYS = ("apps", "builtins", "code_review_sage", "sage_lib")\n'
        'APP = "code-review-sage"\n'
        'url = f"/api/apps/code-review-sage/{path}"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (3, "code_review_sage"),
        (4, "code-review-sage"),
        (5, "/api/apps/code-review-sage/"),
    ]
