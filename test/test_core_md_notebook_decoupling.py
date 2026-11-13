"""GATE: Kiro Crew core does not depend on the Notes (md-notebook) app's code.

Notes (``md-notebook``) is to leave this repository whole. That is only possible while
nothing outside ``src/kiro_crew/apps/builtins/`` imports it, and while the strings core
keeps that name it are ones the app does not need from core. What the app needs from the
core it imports from the core (the proxy auth, the atomic writer, the git runner, the
state moves in ``md_notebook_state``).

Two properties are pinned:

1. **No core module or script imports the app.** Every module under ``src/kiro_crew``
   outside ``apps/builtins/``, and every script under ``scripts/``, is parsed, every
   import form included; the probes below fail if one stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path built
   from ``"md_notebook"``, or a lookup keyed on the app id ``"md-notebook"``, reaches the
   app as surely as an import does. Docstrings are prose and are not read. Each row left
   is listed in ``KNOWN_NAME_REFERENCES`` with the reason it stays; a new one fails here,
   and so does a listed one that is gone.

The scans are the shared ones in ``test/core_app_independence.py``.

If this goes red because core reached for the app, add a generic, app-keyed seam the app
fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_md_notebook_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.md_notebook"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("md_notebook", "md-notebook")

_LEGACY_TEMP_SWEEP = (
    "SECURITY: removed a legacy md-notebook staging temp at %s. A pre-upgrade writer staged "
    "it beside the state files, where no sandbox mask covers it, so it may have held the "
    "GitHub token in cleartext. Treat that token as exposed to anything that ran as this "
    "user and rotate it if in doubt."
)
_POSTURE_REASON = (
    "Every md-notebook API error body. Handlers drive git and filesystem work over "
    "caller-supplied vault URLs, ids and note paths, so an unmodeled exception carries "
    "absolute paths and git stderr; `_safe_error` scrubs credentials, exfiltration URLs and "
    "host paths before the JSON reaches the browser \N{EM DASH} the same output-boundary "
    "reason as the dashboard error sinks."
)

#: (path relative to ``src/kiro_crew``, or to the repo root for a script; the literal):
#: the core strings that still name the app, each with why it is not a seam. Each row is
#: deleted together with the file, table or layout it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        # -- Earlier layouts of the app's state. Releases before the app kept its token,
        #    registry and settings in ``workspace/md-notebook/`` and then in the top-level
        #    ``md-notebook-staging/``. Those names stay masked, fenced, stubbed and swept
        #    by core, and ``md_notebook_state`` moves what they hold into the app's own
        #    data directory: a host still holding them must never expose the token, and
        #    an app may not choose core's masks for itself. They go when no supported
        #    upgrade path can still hold the old layouts.
        ("md_notebook_state.py", "md-notebook-staging"),
        ("md_notebook_state.py", "md-notebook"),
        ("sandbox_leaves.py", "md-notebook"),
        ("security/paths_lists.py", "workspace/md-notebook/pat"),
        ("security/paths_lists.py", "workspace/md-notebook/vaults.json"),
        ("security/paths_lists.py", "workspace/md-notebook/settings.json"),
        ("security/paths_lists.py", "md-notebook-staging"),
        ("sandbox_staging.py", "cannot remove the legacy md-notebook staging temp "),
        ("sandbox_staging.py", _LEGACY_TEMP_SWEEP),
        ("sandbox_staging.py", "could not resolve the crew data home for md-notebook masking"),
        ("sandbox_staging.py", "the md-notebook state mask target "),
        ("sandbox_staging.py", " to give the md-notebook state mask a mount target: "),
        ("sandbox_staging.py", "cannot re-check the md-notebook state mask target "),
        ("sandbox_staging.py", "could not resolve the crew data home for the md-notebook mask"),
        ("sandbox_staging.py", "could not resolve $HOME for the md-notebook mask"),
        ("sandbox_staging.py", "could not resolve the crew data home for the md-notebook sweep"),
        ("sandbox_staging.py", "could not resolve $HOME for the md-notebook sweep"),
        # -- The shipped backend refuses to start while ``workspace/md-notebook`` is reached
        #    through a link, because the old layout's degraded mask would swallow its writes.
        (
            "apps/backend.py",
            "App %s: not starting: workspace/md-notebook is reached through a link, so its "
            "writes would be lost on restart; replace the link with a real directory",
        ),
        # -- The security-posture tables classify named files of the package, built-in
        #    apps included: the core's audit of the app's code, not a reach into it.
        ("security_posture_sinks_gateway.py", "md-notebook error middleware"),
        ("security_posture_sinks_gateway.py", "apps/builtins/md_notebook/server_http.py"),
        ("security_posture_sinks_gateway.py", _POSTURE_REASON),
    }
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert (
        not offenders
    ), "core imports the md-notebook app; add a generic seam the app fills instead:\n" + "\n".join(
        offenders
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    pinned = KNOWN_NAME_REFERENCES
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, pinned)
    assert not unexpected, "a core string names the md-notebook app:\n" + "\n".join(unexpected)
    assert found == pinned, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(pinned - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.md_notebook.server", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.md_notebook import git_ops", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import md_notebook", "kiro_crew.x", False),
        ("from ..apps.builtins.md_notebook import notes", "kiro_crew.dashboard.y", False),
        ("from .builtins.md_notebook import server_store", "kiro_crew.apps.y", False),
        ("from .builtins import md_notebook", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.md_notebook.server')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.md_notebook')", "kiro_crew.x", False),
    ],
)
def test_the_scan_catches_every_import_form(source, importer, is_package):
    assert independence.app_imports(ast.parse(source), APP_PACKAGE, importer, is_package)


@pytest.mark.parametrize(
    "source",
    [
        "from kiro_crew import md_notebook_state",
        "from kiro_crew.apps.builtins import BUILTIN_NAMES",
        "import kiro_crew.apps.builtins.md_notebook_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_or_ids():
    tree = ast.parse(
        '"""Mentions md-notebook in prose."""\n'
        'LEAF = "md-notebook-staging"\n'
        'APP = "md-notebook"\n'
        'mod = "kiro_crew.apps.builtins.md_notebook.server"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (2, "md-notebook-staging"),
        (3, "md-notebook"),
        (4, "kiro_crew.apps.builtins.md_notebook.server"),
    ]
