"""GATE: Kiro Crew core does not depend on the Issue Radar (``issue-radar``) app's code.

Issue Radar is to leave this repository whole. That is only possible while nothing
outside ``src/kiro_crew/apps/builtins/`` imports it, and while every core string that
still names it is one somebody decided to keep. What the app needs from the core it
imports from the core. The app's pins of core parity (the shared ``gh`` runner's URL
parser and error class, its MCP tools' app id, its config lock's mode, its crew-log-off
refusal) run in the app's own ``tests/``.

Two properties are pinned:

1. **No core module or script imports the app.** Every module under
   ``src/kiro_crew`` outside ``apps/builtins/``, and every script under
   ``scripts/``, is parsed. ``import``, ``from ... import`` (relative ones
   resolved), ``from kiro_crew.apps.builtins import issue_radar``,
   ``importlib.import_module`` and ``__import__`` with a literal name are all in
   scope, and the probes below fail if any of those forms stops being caught.
2. **The strings that still name the app are a pinned, shrinking list.** A path
   built from ``"issue_radar"``, or a route or tool keyed on the app id
   ``"issue-radar"``, reaches the app as surely as an import does. Docstrings are
   prose and are not read. The rows left are listed in ``KNOWN_NAME_REFERENCES``
   with the reason each stays; a new one fails here, and so does a listed one that
   is gone.

The scans are the shared ones in ``test/core_app_independence.py``. The dashboard
half is ``website/src/apps/issue-radar/issueRadarBoundary.test.ts``.

If this goes red because core reached for the app, add a generic, app-keyed seam
the app fills instead. Do not add an exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import core_app_independence as independence
import pytest

from kiro_crew import security_posture_sinks_gateway
from kiro_crew.crew_log import entry_types_ledgers

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_core_issue_radar_decoupling")

APP_PACKAGE = "kiro_crew.apps.builtins.issue_radar"
#: The package directory name and the app id: every spelling that addresses the app.
APP_NAMES = ("issue_radar", "issue-radar")

_TOOLS = ("issue_radar_record_investigation", "issue_radar_crew_read", "issue_radar_crew_record")
_CREW_ROUTES = ("/api/apps/issue-radar/crew", "/api/apps/issue-radar/crew/work")
_INVESTIGATION_ROUTE = "/api/apps/issue-radar/investigation"

#: The ``radar/recorded`` entry's note, which names the tool that writes the entry.
_RADAR_ENTRY_NOTE = next(
    t.note for t in entry_types_ledgers.TYPES if t.type == entry_types_ledgers.RADAR_ENTRY_TYPE
)
#: The reason text of the provider-CLI-stderr sink row, which names the app's routes.
_CLI_STDERR_REASON = next(
    reason
    for _title, path, reason in security_posture_sinks_gateway._GATEWAY_SINKS
    if path == "apps/builtins/issue_radar/backend/errors.py"
)

#: (path relative to ``src/kiro_crew``, the literal): the core strings that still
#: name the app, each with why it is not a seam. Each row is deleted together with
#: the tool, the route, the file or the doc it names.
KNOWN_NAME_REFERENCES: frozenset[tuple[str, str]] = frozenset(
    {
        # The app's three agent tools run in the ``kirocrew-core`` MCP server, which
        # holds the gateway's internal secret, and the gateway admits exactly their
        # three routes to internal-secret callers. ``apps.builtin_agent_tools`` is the
        # seam a built-in declares such a tool through, but it is data only by design
        # (the handler is the core's, every argument a bounded string, the route under
        # the app's ``/apps/<app>/api`` proxy namespace), and these handlers resolve a
        # crew from a strictly-identified session, take integer and list arguments,
        # sanitize a public progress line and post to the in-process
        # ``/api/apps/issue-radar`` routes. Moving them out needs a seam that lets a
        # built-in ship handler CODE into the credential-holding server and admit
        # in-process routes, which is a trust-model decision for a maintainer, not a
        # refactor. Until then the descriptors, handlers, ``TOOL_APPS`` rows,
        # validation schemas and internal-secret path rows stay, unchanged.
        *(("mcp_tools/apps.py", tool) for tool in _TOOLS),
        ("mcp_tools/apps.py", "issue-radar"),
        ("mcp_tools/apps.py", _INVESTIGATION_ROUTE),
        *(("validation_parts/app_schemas.py", tool) for tool in _TOOLS),
        *(("validation_parts/server_registries.py", tool) for tool in _TOOLS),
        ("dashboard/server_internal_paths.py", _INVESTIGATION_ROUTE),
        *(("dashboard/server_internal_paths.py", route) for route in _CREW_ROUTES),
        *(("mcp_core.py", route) for route in _CREW_ROUTES),
        # The ``radar/recorded`` crew-log entry type is the crew ledger's storage, and
        # its note names the tool above that writes it. It goes with the ``radar`` fold.
        ("crew_log/entry_types_ledgers.py", _RADAR_ENTRY_NOTE),
        # The work-dir sweep's own table of the one-run session-key shapes whose
        # derived directories it may delete. An app may not declare what the core
        # deletes, so the app's four tool-less model-call keys stay listed here.
        ("session_work_dir.py", "issue-radar-ai"),
        ("session_work_dir.py", "issue-radar-pr-ai"),
        ("session_work_dir.py", "issue-radar-reco"),
        ("session_work_dir.py", "issue-radar-tagging"),
        # The packaged user doc ``docs/issue-radar-pipeline.md`` is a tip source like
        # every other packaged doc; the row goes with the doc.
        ("tips_allowlist.py", "issue-radar-pipeline.md"),
        # The security-posture tables classify named files of the package, built-in
        # apps included: the core's audit of the app's code, not a reach into it.
        ("security_posture.py", "apps/builtins/issue_radar/backend/crew_runtime_session.py"),
        ("security_posture.py", "apps/builtins/issue_radar/backend/http_routes/ai.py"),
        (
            "security_posture.py",
            "apps/builtins/issue_radar/backend/http_routes/recommendations.py",
        ),
        ("security_posture.py", "apps/builtins/issue_radar/backend/http_routes/tagging.py"),
        (
            "security_posture_sinks_delivery.py",
            "apps/builtins/issue_radar/backend/pipeline_fold.py",
        ),
        (
            "security_posture_sinks_gateway.py",
            "apps/builtins/issue_radar/backend/azure_client_writes.py",
        ),
        ("security_posture_sinks_gateway.py", "apps/builtins/issue_radar/backend/errors.py"),
        ("security_posture_sinks_gateway.py", _CLI_STDERR_REASON),
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
    ), "core imports the Issue Radar app; add a generic seam the app fills instead:\n" + "\n".join(
        offenders
    )


def test_the_strings_naming_the_app_are_the_pinned_list(core_trees):
    unexpected, found = independence.core_name_strings(core_trees, APP_NAMES, KNOWN_NAME_REFERENCES)
    assert not unexpected, "a core string names the Issue Radar app:\n" + "\n".join(unexpected)
    assert found == KNOWN_NAME_REFERENCES, (
        "a pinned reference is gone; delete its row from KNOWN_NAME_REFERENCES: "
        f"{sorted(KNOWN_NAME_REFERENCES - found)}"
    )


def test_the_computed_rows_still_name_the_app():
    """The two rows read from their tables are real hits, not empty lookups."""
    assert "issue_radar_crew_record" in _RADAR_ENTRY_NOTE
    assert "issue-radar routes" in _CLI_STDERR_REASON


@pytest.mark.parametrize(
    ("source", "importer", "is_package"),
    [
        ("import kiro_crew.apps.builtins.issue_radar.backend.routes", "kiro_crew.x", False),
        (
            "from kiro_crew.apps.builtins.issue_radar.backend import crew_store",
            "kiro_crew.x",
            False,
        ),
        ("from kiro_crew.apps.builtins import issue_radar", "kiro_crew.x", False),
        ("from ..apps.builtins.issue_radar.backend import store", "kiro_crew.dashboard.y", False),
        ("from .builtins.issue_radar.backend import routes", "kiro_crew.apps.y", False),
        ("from .builtins import issue_radar", "kiro_crew.apps", True),
        (
            "import importlib\n"
            "importlib.import_module('kiro_crew.apps.builtins.issue_radar.backend.crew_runtime')",
            "kiro_crew.x",
            False,
        ),
        ("__import__('kiro_crew.apps.builtins.issue_radar')", "kiro_crew.x", False),
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.apps.builtins.issue_radar.backend import crew_store",
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
        "import kiro_crew.apps.builtins.issue_radar_other",
    ],
)
def test_the_scan_leaves_other_imports_alone(source):
    assert not independence.app_imports(ast.parse(source), APP_PACKAGE, "kiro_crew.x", False)


def test_the_string_scan_skips_docstrings_but_not_routes_tools_or_paths():
    tree = ast.parse(
        '"""Mentions issue-radar in prose."""\n'
        "# the issue_radar crew tools\n"
        'TOOL = "issue_radar_crew_read"\n'
        'ROUTE = f"/api/apps/issue-radar/{leaf}"\n'
        'FILE = ("apps", "builtins", "issue_radar", "backend")\n'
    )
    assert sorted(independence.name_strings(tree, APP_NAMES)) == [
        (3, "issue_radar_crew_read"),
        (4, "/api/apps/issue-radar/"),
        (5, "issue_radar"),
    ]
