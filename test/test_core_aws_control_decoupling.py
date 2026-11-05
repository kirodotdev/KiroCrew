"""GATE: Kiro Crew core does not depend on the aws-control app's code.

The aws-control app, with its crew runtime, bundle builder and image build
scripts, is leaving this repository whole. That is only possible while nothing
outside ``src/kiro_crew/apps/builtins/`` imports it. A core feature that needs
something the app ships asks a seam in :mod:`kiro_crew.apps.seams`, and the app
fills it from its own ``seams.py``. The scans are the shared ones in
``test/core_app_independence.py``.

Two properties are pinned:

1. **No core module imports the app.** Every module under ``src/kiro_crew``
   outside ``apps/builtins/``, and every script under ``scripts/``, is parsed.
   ``import``, ``from ... import`` (relative ones resolved), ``from
   kiro_crew.apps.builtins import aws_control``, ``importlib.import_module`` and
   ``__import__`` with a literal name are all in scope, and the probes below fail
   if any of those forms stops being caught.
2. **The strings that still name the package are a pinned, shrinking list.** A
   path built from ``"aws_control"`` reaches the app as surely as an import does.
   The rows left are security tables keyed by the app's file paths or audit id.
   They are listed in ``KNOWN_NAME_REFERENCES`` with the reason each stays, and
   each is deleted in the PR that removes the app. A new one fails here, and so
   does a listed one that is gone.

If this goes red because you imported the app from core, invert the dependency
through a seam instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_aws_control_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.aws_control"
APP_NAME = "aws_control"

#: (path relative to ``src/kiro_crew``, the literal): the string constants outside
#: the app that still name its package, each with why it is not a seam. These are
#: the sensitive-store read allowlist (an app may not add itself to it) and the
#: security-posture tables that exempt or classify named app files (core's audit
#: of the app's code). Each row is deleted together with the file it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        ("hooks.py", "aws_control.conversation_export"),
        (
            "security_posture.py",
            "apps/builtins/aws_control/backend/backup_parts/conversations.py",
        ),
        ("security_posture.py", "apps/builtins/aws_control/backend/backup_parts/egress_text.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/backup_parts/nightly.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/backup_parts/retention.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/routes_parts/common.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/routes_parts/drive.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/library.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/storage.py"),
        ("security_posture.py", "apps/builtins/aws_control/backend/accounts.py"),
        ("security_posture_sinks_delivery.py", "apps/builtins/aws_control/backend/routes.py"),
        ("security_posture_sinks_gateway.py", "apps/builtins/aws_control/backend/backup.py"),
    }
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the package name at all."""
    return independence.core_trees([APP_NAME])


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the aws-control app; ask a seam in kiro_crew.apps.seams instead:\n"
        + "\n".join(offenders)
    )


def test_the_strings_naming_the_package_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(
        core_trees, [APP_NAME], KNOWN_NAME_REFERENCES
    )
    assert (
        not unexpected
    ), "a core string names the aws-control package; reach it through a seam:\n" + "\n".join(
        unexpected
    )
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.aws_control.backend.accounts", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.aws_control import seams", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import aws_control", "kiro_crew.x", False),
        ("from ..apps.builtins.aws_control import seams", "kiro_crew.cloud.y", False),
        ("from .builtins.aws_control import seams", "kiro_crew.apps.y", False),
        ("from .builtins import aws_control", "kiro_crew.apps", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.apps.builtins.aws_control.x')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.aws_control')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.aws_control.backend import storage",
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
        "import kiro_crew.apps.builtins.aws_control_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths():
    tree = ast.parse(
        '"""Mentions aws_control in prose."""\n'
        'RUNTIME = ("apps", "builtins", "aws_control", "crew")\n'
    )
    assert [literal for _line, literal in independence.name_strings(tree, [APP_NAME])] == [
        "aws_control"
    ]
