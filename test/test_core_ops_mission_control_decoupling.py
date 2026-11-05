"""GATE: Kiro Crew core does not depend on the Ops Mission Control app's code.

Ops Mission Control (``ops-mission-control``) is to leave this repository whole. That
is only possible while nothing outside ``src/kiro_crew/apps/builtins/`` imports it. What
the app needs from the core it imports from the core (the governance gate, the
notification bus, the redactor, the hidden-leaf staging); its tests of those pins run
in the app's own ``tests/``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import ops_mission_control``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path
   built from ``"ops_mission_control"``, or a lookup keyed on the app id
   ``"ops-mission-control"``, reaches the app as surely as an import does.
   Docstrings are prose and are not read. The rows left are listed in
   ``KNOWN_NAME_REFERENCES`` with the reason each stays; a new one fails here, and
   so does a listed one that is gone.

The largest group is the ``ops_mission_control_api`` MCP tool. It is the app's only
credentialed agent path: a GET/POST dispatcher over an allowlist of the app's
``/api/apps/ops-mission-control`` routes, with a query, a JSON body, a per-operation
channel deny and the internal-secret admission of exactly those routes. The App Kit
seam for a built-in's agent tool (``apps.builtin_agent_tools``) carries only a single
POST route under the app proxy, so moving this tool there would need that seam
widened first. Until then the tool, its validation schema, its admitted paths, its
channel deny row and its title stay core rows, unchanged for users.

The scans are the shared ones in ``test/core_app_independence.py``. The dashboard
half is ``website/src/apps/ops-mission-control/opsMissionControlBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam
the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_ops_mission_control_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.ops_mission_control"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("ops_mission_control", "ops-mission-control")
TOOL = "ops_mission_control_api"

#: The app routes ``dashboard/server_internal_paths.py`` admits to internal-secret
#: callers for the MCP tool, each spelled out (never the app prefix).
_ADMITTED_ROUTES = (
    "state",
    "signals",
    "incidents",
    "handover",
    "rotation",
    "ledger",
    "dispatch",
    "incident/transition",
    "incident/claim",
    "incident/action",
)


def _tool_descriptor_texts() -> frozenset[tuple[str, str]]:
    """The tool's model-facing description and its ``path`` description, as rows.

    Read from the live descriptor rather than restated here: the text is the
    tool's own, and a restated copy would only go stale. Any OTHER string in
    ``mcp_tools/apps.py`` naming the app still fails.
    """
    from kiro_crew.mcp_tools import apps

    descriptor = next(d for d in apps.schemas() if d["name"] == TOOL)
    return frozenset(
        ("mcp_tools/apps.py", text)
        for text in (
            descriptor["description"],
            descriptor["inputSchema"]["properties"]["path"]["description"],
        )
    )


#: (path relative to ``src/kiro_crew``, or to the repo root for a script; the
#: literal): the core strings that still name the app, each with why it is not a
#: seam. Each row is deleted together with the file, table or tool it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        # -- The ``ops_mission_control_api`` MCP tool (see the module docstring). Its
        #    descriptor, ``TOOL_APPS`` row (hidden while the app is off), handler and
        #    refusal text; the ``MCP_CORE_SCHEMAS`` validation row; the per-operation
        #    channel deny of ``POST /rotation/arm``; and the exact internal-secret
        #    admission of the routes the tool reaches. ``apps.builtin_adoption_checks``
        #    reads ``TOOL_APPS`` and the channel table, so these rows also keep the
        #    app from being adopted from its own repository before an app-contributed
        #    tool seam carries them.
        ("mcp_tools/apps.py", TOOL),
        ("mcp_tools/apps.py", "ops-mission-control"),
        ("mcp_tools/apps.py", "/api/apps/ops-mission-control"),
        ("mcp_tools/apps.py", " is not part of the ops-mission-control agent surface."),
        ("validation_parts/app_schemas.py", TOOL),
        ("validation_parts/server_registries.py", TOOL),
        ("channel.py", TOOL),
        *(
            ("dashboard/server_internal_paths.py", f"/api/apps/ops-mission-control/{route}")
            for route in _ADMITTED_ROUTES
        ),
        # -- Governance vocabulary. The ``webhooks`` scope's source ids are the
        #    core's: a policy names ``ops-mission-control`` to permit the app's signal
        #    webhook, and an app may not choose which id it is governed under.
        ("webhook_sources.py", "ops-mission-control"),
        # -- The sandbox's leaf tables. The app's credential store and autonomy
        #    ceiling are HIDDEN / READONLY crew-home leaves, and its rotation schedule
        #    and incident index are write-protected. These are the core's ceiling
        #    over the agent, which an app may not write for itself.
        ("sandbox_aliases.py", "ops_mission_control_secrets.json"),
        ("sandbox_leaves.py", "ops_mission_control_secrets.json"),
        ("sandbox_leaves.py", "ops_mission_control_policy.json"),
        ("security/paths_lists.py", "ops_mission_control_secrets.json"),
        ("security/paths_lists.py", "ops_mission_control_policy.json"),
        ("security/paths_lists.py", "/apps/ops-mission-control/data/rotation.yaml"),
        ("security/paths_lists.py", "/apps/ops-mission-control/data/incidents/index.json"),
        # -- The security-posture tables classify named files of the package, built-in
        #    apps included: the core's audit of the app's code, not a reach into it.
        ("security_posture.py", "apps/builtins/ops_mission_control/backend/secrets.py"),
        (
            "security_posture.py",
            "apps/builtins/ops_mission_control/backend/providers/github_issues.py",
        ),
        ("security_posture.py", "apps/builtins/ops_mission_control/backend/providers/http.py"),
        *(
            ("security_posture_sinks_delivery.py", f"apps/builtins/ops_mission_control/{rel}")
            for rel in (
                "backend/slack_out.py",
                "backend/registry.py",
                "backend/ledger_sync.py",
                "backend/dispatch.py",
                "backend/routes.py",
                "backend/notify_out.py",
                "backend/store.py",
            )
        ),
        # -- The app's chat skill ships in ``builtin_skills/ops-mission-control/`` like
        #    every other packaged skill, so the closed skill-id list names it;
        #    ``test_feature_usage`` pins that list equal to the tree.
        ("metrics/feature_usage.py", "ops-mission-control"),
        # -- The docs lint's exact allowlist of runtime-data paths a module spec cites:
        #    ``security.md`` names the two files the write-protect table above holds,
        #    and they never exist in a checkout.
        ("scripts/docs_lint.py", "apps/ops-mission-control/data/rotation.yaml"),
        ("scripts/docs_lint.py", "apps/ops-mission-control/data/incidents/index.json"),
    }
)


@pytest.fixture(scope="module")
def core_trees() -> dict[Path, ast.AST]:
    """The parsed core files that mention the app at all."""
    return independence.core_trees(APP_NAMES)


def test_no_core_module_or_script_imports_the_app(core_trees):
    offenders = independence.core_app_imports(core_trees, APP_PACKAGE)
    assert not offenders, (
        "core imports the ops-mission-control app; add a generic seam the app fills "
        "instead:\n" + "\n".join(offenders)
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    pinned = KNOWN_NAME_REFERENCES | _tool_descriptor_texts()
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, pinned)
    assert not unexpected, "a core string names the ops-mission-control app:\n" + "\n".join(
        unexpected
    )
    assert found == pinned, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(pinned - found)}"
    )


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        (
            "import kiro_crew.apps.builtins.ops_mission_control.backend.routes",
            "kiro_crew.x",
            False,
        ),
        ("from kiro_crew.apps.builtins.ops_mission_control import backend", "kiro_crew.x", False),
        ("from kiro_crew.apps.builtins import ops_mission_control", "kiro_crew.x", False),
        (
            "from ..apps.builtins.ops_mission_control.backend import store",
            "kiro_crew.dashboard.y",
            False,
        ),
        ("from .builtins.ops_mission_control.backend import store", "kiro_crew.apps.y", False),
        ("from .builtins import ops_mission_control", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.ops_mission_control.backend')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.ops_mission_control')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.ops_mission_control.backend import models",
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
        "import kiro_crew.apps.builtins.ops_mission_control_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_paths_ids_or_urls():
    tree = ast.parse(
        '"""Mentions ops-mission-control in prose."""\n'
        'LEAF = "ops_mission_control_policy.json"\n'
        'APP = "ops-mission-control"\n'
        'url = f"/api/apps/ops-mission-control/{route}"\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (2, "ops_mission_control_policy.json"),
        (3, "ops-mission-control"),
        (4, "/api/apps/ops-mission-control/"),
    ]
