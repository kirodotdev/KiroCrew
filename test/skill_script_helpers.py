"""Load a checked-in skill script as a module without leaving bytecode behind.

The kirocrew-prepare-pr scripts live inside the checked-out source tree, so importing one
the ordinary way drops a ``__pycache__`` entry beside it that outlives the run --
a persistent mutation of the working copy, which the no-test-side-effects rule
forbids.

Five test modules each grew their own loader and none of them carried the guard.
Measured one file at a time from a clean tree, they left this in
``src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/__pycache__/``:

===================================  ==========================================
test module                          residue
===================================  ==========================================
``test_push_guard``                  ``push_guard.pyc``
``test_prepare_pr_status``           ``pr_status.pyc``
``test_prepare_pr_profiles``         ``pr_status.pyc``, ``resolve_profile.pyc``
``test_prepare_pr_local_review``     ``local_review.pyc``, ``resolve_profile.pyc``
``test_prepare_pr_findings``         ``pr_findings.pyc``, ``pr_status.pyc``
===================================  ==========================================

One helper rather than six copies of a three-line guard, because a guard that has
to be remembered at each call site is one that will be missing at the seventh.
``test_prepare_pr_prove`` had it and the others did not, which is exactly how
that shape fails.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterator


@contextlib.contextmanager
def no_bytecode() -> Iterator[None]:
    """Disable bytecode writing for imports performed inside the block.

    For call sites that import by NAME off ``sys.path`` (or reload), where there
    is no spec to hand to :func:`load_skill_script`. Restores the previous value
    rather than clearing it, so nesting and a caller that deliberately enables
    writing both survive.
    """
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def load_skill_script(module_name: str, path: Path | str) -> ModuleType:
    """Import the script at ``path`` under ``module_name``, writing no bytecode.

    Not registered in ``sys.modules``: these scripts are loaded for unit-level
    assertions on their helpers, and several test modules load the SAME script
    under different names, so registering would let one test's copy answer
    another's import.
    """
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path} as {module_name}")
    module = importlib.util.module_from_spec(spec)
    with no_bytecode():
        spec.loader.exec_module(module)
    return module


# Keys a FLAT check-row fixture may carry for the fields the host nests under the
# check suite. ``rollup_graphql_response`` nests them the way GitHub does, and the
# scripts' ``flatten_rollup_row`` lifts them back out, so a fixture is written in
# the shape the scripts consume while the fake answers in the shape the host sends.
_SUITE_KEYS = (
    "workflowName",
    "workflowRunId",
    "workflowRunEvent",
    "workflowDefinitionId",
    "workflowRunConclusion",
)


def rollup_graphql_response(
    checks: list[dict[str, object]],
    head: str,
    *,
    commit_oid: str | None = None,
    has_next: bool = False,
    end_cursor: str | None = None,
) -> str:
    """One page of the ``statusCheckRollup`` GraphQL read, built from FLAT rows.

    A row carrying ``context`` becomes a ``StatusContext`` node; any other row a
    ``CheckRun``. A check row naming none of the suite-level keys models a check
    run created outside Actions: its ``checkSuite`` has no ``workflowRun``, which
    is the shape the CodeQL app's row takes on this host. A row naming any of them
    gets a ``workflowRun``, with the keys it omits left ``null`` -- the nullable
    ``databaseId`` scalars, or a response the host truncated.

    ``commit_oid`` defaults to ``head``; pass a different value to model the
    rollup's own commit disagreeing with the pull request's reported head.
    """
    nodes: list[dict[str, object]] = []
    for check in checks:
        if check.get("context") is not None:
            nodes.append(
                {
                    "__typename": "StatusContext",
                    "context": check.get("context"),
                    "state": check.get("state"),
                    "targetUrl": check.get("targetUrl"),
                    "createdAt": check.get("createdAt"),
                }
            )
            continue
        node: dict[str, object] = {
            "__typename": "CheckRun",
            "name": check.get("name"),
            "status": check.get("status"),
            "conclusion": check.get("conclusion"),
            "startedAt": check.get("startedAt"),
            "detailsUrl": check.get("detailsUrl"),
        }
        if any(key in check for key in _SUITE_KEYS):
            node["checkSuite"] = {
                "conclusion": check.get("workflowRunConclusion"),
                "workflowRun": {
                    "databaseId": check.get("workflowRunId"),
                    "event": check.get("workflowRunEvent"),
                    "workflow": {
                        "databaseId": check.get("workflowDefinitionId"),
                        "name": check.get("workflowName"),
                    },
                },
            }
        else:
            node["checkSuite"] = {"conclusion": None, "workflowRun": None}
        nodes.append(node)
    page = {"hasNextPage": has_next, "endCursor": end_cursor}
    commit = {
        "oid": head if commit_oid is None else commit_oid,
        "statusCheckRollup": {"contexts": {"pageInfo": page, "nodes": nodes}},
    }
    pull_request = {"headRefOid": head, "commits": {"nodes": [{"commit": commit}]}}
    return json.dumps({"data": {"repository": {"pullRequest": pull_request}}})


def is_rollup_graphql_read(args: list[str]) -> bool:
    """Whether a faked ``run`` call is the scripts' rollup read.

    The scripts issue several ``gh api graphql`` reads (review threads, comment
    edit history); only the rollup one names ``statusCheckRollup`` in its query.
    """
    return args[:3] == ["gh", "api", "graphql"] and any(
        a.startswith("query=") and "statusCheckRollup" in a for a in args[3:]
    )
