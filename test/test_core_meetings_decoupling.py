"""GATE: Kiro Crew core does not depend on the Meetings (``meetings``) app's code.

Meetings is to leave this repository whole. That is only possible while nothing
outside ``src/kiro_crew/apps/builtins/`` imports it or names it. What the app needs
from the core it imports from the core (speech-to-text, the untrusted-calendar
fence, the redaction and sandbox helpers); the core needs nothing from the app. The
app's pins of core behaviour (the startup sweep's store row and the transcript's
owner-only mode, the import suffixes' forced demuxers, live translation's start
priority) run in the app's own ``tests/``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import meetings``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path
   built from ``"meetings"``, or a lookup keyed on the app id, reaches the app as
   surely as an import does. Docstrings are prose and are not read. The app id is
   also its package directory name and an ordinary English word, so the list also
   carries the core strings that say "meetings" about something else. Each row is
   in ``KNOWN_NAME_REFERENCES`` with the reason it stays; a new one fails here, and
   so does a listed one that is gone.

The scans are the shared ones in ``test/core_app_independence.py``. The dashboard
half is ``website/src/apps/meetings/meetingsBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam
the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_meetings_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.meetings"
#: The package directory name, which is also the app id: the one spelling that
#: addresses the app. Its agents (``meetings-note-taker`` and the rest) contain it.
APP_NAMES = ("meetings",)

#: (path relative to ``src/kiro_crew``, the literal): the core strings that still
#: name the app, or say the word about something else, each with why it is not a
#: seam. A row about the app is deleted together with the file, store or skill it
#: names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        # The app's chat skill ships in ``builtin_skills/meetings/`` like every other
        # packaged skill, so the closed skill-id list names it; ``test_feature_usage``
        # pins that list equal to the tree.
        ("metrics/feature_usage.py", "meetings"),
        # The data-home security floors name the app's stores by path: the startup
        # sweep's owner-only store, the HIDDEN sandbox leaf and file-tool fence of the
        # owner's edits, and the write fence of the owner's notes. These are the
        # core's governance of the data home, which an app may not write for itself,
        # and moving them behind a seam would change the sandbox scope.
        ("owner_only_files.py", "apps/meetings/data"),
        ("sandbox_leaves.py", "apps/meetings/data/edits"),
        ("security/paths_lists.py", "apps/meetings/data/edits"),
        ("security/paths_lists.py", "/apps/meetings/data/notes"),
        # The security-posture tables classify named files of the package, built-in
        # apps included: the core's audit of the app's code, not a reach into it.
        *(
            ("security_posture.py", f"apps/builtins/meetings/backend/{module}")
            for module in (
                "domain/session.py",
                "domain/translate.py",
                "providers/calendar.py",
                "providers/tasks.py",
                "routes/_common.py",
                "routes/agents.py",
                "routes/meeting_lifecycle.py",
                "routes/tasks.py",
            )
        ),
        # Not the app: the Zoom connector's operation ids name Zoom's own
        # ``meetings`` API resource.
        *(
            ("connections/vendors/zoom/paging.py", f"zoom.meetings.{operation}")
            for operation in (
                "list",
                "past_participants",
                "registrants",
                "get",
                "recurring_instances",
                "ai_summary",
            )
        ),
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
    ), "core imports the Meetings app; add a generic seam the app fills instead:\n" + "\n".join(
        offenders
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, KNOWN_NAME_REFERENCES)
    assert not unexpected, "a core string names the Meetings app:\n" + "\n".join(unexpected)
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.meetings.backend.routes", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins.meetings.backend import store", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import meetings", "kiro_crew.x", False),
        ("from ..apps.builtins.meetings.backend import constants", "kiro_crew.dashboard.y", False),
        ("from .builtins.meetings.backend import store", "kiro_crew.apps.y", False),
        ("from .builtins import meetings", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.meetings.backend.constants')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.meetings')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.meetings.backend.domain import session\n",
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
        "import kiro_crew.apps.builtins.meetings_other",
        "from kiro_crew.connections.vendors.zoom import paging",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_ids_or_agents():
    tree = ast.parse(
        '"""Mentions meetings in prose."""\n'
        'LEAF = "apps/meetings/data/edits"\n'
        'APP = "meetings"\n'
        'AGENT = "meetings-note-taker"\n'
        'url = f"/api/apps/meetings/{path}"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (2, "apps/meetings/data/edits"),
        (3, "meetings"),
        (4, "meetings-note-taker"),
        (5, "/api/apps/meetings/"),
    ]
