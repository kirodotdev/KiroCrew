"""Response fixtures and fake runners that the GitHub pull-request monitor test files share.

Holds ``_HEAD``, the flat ``_primary`` and ``_check_run`` payloads, the wire-shape builders
(``_pr_node``, ``_rollup_node``, ``_threads_node``, ``_envelope``, ``_threads``,
``_comments``), the canned-response ``_provider`` with its ``_FakeRunner``, the hand-built
``_CompletedRunner`` with ``_completed`` and ``_core_and_rollup``, and ``_probe_one``. Each
of the four ``test_github_pull_request_monitor*.py`` files imports what it reads by name.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from copy import deepcopy

from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProvider

_HEAD = "0123456789abcdef0123456789abcdef01234567"


def _probe_one(
    provider: GitHubPullRequestProvider,
    target: str = "https://github.com/owner/repo/pull/123",
    *,
    previous_observation: object = None,
) -> object:
    """Probe ONE subject across the plural boundary and return its result.

    The tests calling it are about what the GitHub probe derives from a response, not
    about the boundary's arity -- that is covered on its own in
    ``TestPluralProbeBoundary``. Wrapping the one-element call once keeps every
    caller's assertion reading as it did.
    """
    previous = None if previous_observation is None else {target: previous_observation}
    return provider.probe((target,), previous_observations=previous)[target]


def _primary(**changes: object) -> dict[str, object]:
    """One pull request in the fixtures' own flat vocabulary.

    Flat because a test reads and overrides it far more often than the wire shape
    it becomes: ``_envelope`` and ``_rollup_node`` are the single translators that
    turn this into what GitHub actually answers, so no test hand-writes the
    nesting and no fixture can drift from the selection the provider asks for.
    """
    payload: dict[str, object] = {
        "number": 123,
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": _HEAD,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            _check_run(),
            {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"},
        ],
    }
    payload.update(changes)
    return payload


def _pr_node(payload: Mapping[str, object]) -> dict[str, object]:
    """The primary read's node: the same fields, minus the rollup it never selects."""
    return {key: value for key, value in payload.items() if key != "statusCheckRollup"}


def _wire_check_row(row: object) -> object:
    """Nest a flat fixture row the way GitHub returns it.

    A ``CheckRun``'s workflow name, its run's id, its run's conclusion and its
    workflow definition's id are all reached through its check suite on the wire, so
    the flat ``workflowName``, ``workflowRunId``, ``workflowRunConclusion`` and
    ``workflowDefinitionId`` a test writes are moved there rather than sent as fields
    GitHub never returns. Any other row
    is passed through untouched, which is what keeps the malformed-row tests
    testing malformed rows.

    Every key the collapse reads has to be nested here, or the fixtures hand the
    normalizer a shape the host never sends and the real extraction in
    ``_flat_check_row`` stops being exercised: deleting it outright would leave every
    collapse test green. ``workflowRunConclusion`` was flat at first and did exactly
    that, which is why it is listed below.
    """
    if not isinstance(row, dict) or row.get("__typename") != "CheckRun":
        return row
    flattened = {
        "workflowName",
        "workflowRunId",
        "workflowDefinitionId",
        "workflowRunCreatedAt",
        "workflowRunEvent",
        "workflowRunConclusion",
    }
    nested = {key: value for key, value in row.items() if key not in flattened}
    if flattened & set(row):
        run: dict[str, object] = {}
        if "workflowRunId" in row:
            run["databaseId"] = row["workflowRunId"]
        if "workflowRunCreatedAt" in row:
            run["createdAt"] = row["workflowRunCreatedAt"]
        if "workflowRunEvent" in row:
            run["event"] = row["workflowRunEvent"]
        workflow: dict[str, object] = {}
        if "workflowName" in row:
            workflow["name"] = row["workflowName"]
        if "workflowDefinitionId" in row:
            workflow["databaseId"] = row["workflowDefinitionId"]
        if workflow:
            run["workflow"] = workflow
        suite: dict[str, object] = {"workflowRun": run}
        if "workflowRunConclusion" in row:
            suite["conclusion"] = row["workflowRunConclusion"]
        nested["checkSuite"] = suite
    return nested


def _rollup_node(
    payload: Mapping[str, object],
    *,
    commit_oid: str | None = None,
    total: int | None = None,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    """The supplemental check read's node, carrying the head it describes."""
    rows = payload.get("statusCheckRollup")
    head = payload.get("headRefOid")
    rollup: object = None
    if rows is not None:
        rollup = {
            "contexts": {
                "totalCount": (
                    (len(rows) if isinstance(rows, list) else 0) if total is None else total
                ),
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": [_wire_check_row(row) for row in rows] if isinstance(rows, list) else rows,
            }
        }
    return {
        "headRefOid": head,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head if commit_oid is None else commit_oid,
                        "statusCheckRollup": rollup,
                    }
                }
            ]
        },
    }


def _threads_node(
    nodes: Sequence[object] | None = None,
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    """The supplemental review-thread read's node for one subject."""
    normalized_nodes: list[object] = []
    source_nodes = list(nodes) if nodes is not None else [{"isResolved": True}] * 2
    for node in source_nodes:
        if isinstance(node, dict) and "isOutdated" not in node:
            node = {**node, "isOutdated": False}
        normalized_nodes.append(node)
    return {
        "reviewThreads": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": normalized_nodes,
        }
    }


def _envelope(*nodes: object, errors: Sequence[object] | None = None) -> dict[str, object]:
    """One batched GraphQL response, one alias per subject in the order asked.

    A ``None`` node is GitHub answering that the subject is not there, which is
    the shape a real partial failure has: the readable aliases stay populated
    beside it.
    """
    payload: dict[str, object] = {
        "data": {
            f"s{index}": (None if node is None else {"pullRequest": node})
            for index, node in enumerate(nodes)
        }
    }
    if errors is not None:
        payload["errors"] = list(errors)
    return payload


def _threads(
    nodes: Sequence[object] | None = None,
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    """One subject's review-thread read, as a whole response."""
    return _envelope(_threads_node(nodes, has_next=has_next, cursor=cursor))


def _comment_node(
    bodies: Sequence[object] | None = None,
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    """One subject's PR-level (issue) comment read node.

    A string in ``bodies`` becomes a ``{"body": ...}`` comment; a non-string is
    passed through so a malformed-node test can supply its own shape.
    """
    source = list(bodies) if bodies is not None else []
    nodes = [{"body": body} if isinstance(body, str) else body for body in source]
    return {
        "comments": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": nodes,
        }
    }


def _comments(
    bodies: Sequence[object] | None = None,
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    """One subject's PR-level comment read, as a whole response."""
    return _envelope(_comment_node(bodies, has_next=has_next, cursor=cursor))


class _FakeRunner:
    def __init__(self, payloads: Sequence[dict[str, object]]) -> None:
        self._payloads = list(payloads)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        payload = self._payloads.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")


def _reads(payload: Mapping[str, object]) -> list[dict[str, object]]:
    """The responses one subject's primary and check reads produce, in order.

    A terminal pull request issues neither supplemental request, so it contributes
    the primary response alone -- the same rule the provider enforces.
    """
    responses = [_envelope(_pr_node(payload))]
    if payload.get("state") not in {"MERGED", "CLOSED"}:
        responses.append(_envelope(_rollup_node(payload)))
    return responses


def _provider(
    *payloads: dict[str, object],
    pr_comments: dict[str, object] | list[dict[str, object]] | None = None,
) -> tuple[GitHubPullRequestProvider, _FakeRunner]:
    """Wire a provider to canned responses.

    A payload in the fixtures' flat vocabulary (``_primary()``) is expanded into
    the reads it produces; anything else is a whole response handed over verbatim,
    which is how a test supplies its own review-thread pages or an error envelope.
    A live subject with no supplemental response of its own gets the default one,
    so a test that only cares about primary facts does not have to write it.

    The PR-level comment read always follows the thread read, so a live subject
    gets a comment response too: the ``pr_comments`` override when a test cares
    about it, else a default empty page (digest "", so no condition).
    """
    expanded: list[dict[str, object]] = []
    supplemental_supplied = False
    live = False
    for payload in payloads:
        if "state" in payload and "statusCheckRollup" in payload:
            body = deepcopy(payload)
            expanded.extend(_reads(body))
            live = live or body.get("state") not in {"MERGED", "CLOSED"}
        else:
            supplemental_supplied = True
            expanded.append(deepcopy(payload))
    if live and not supplemental_supplied:
        expanded.append(_envelope(_threads_node()))
    if live:
        if pr_comments is None:
            expanded.append(_comments())
        elif isinstance(pr_comments, list):
            expanded.extend(deepcopy(page) for page in pr_comments)
        else:
            expanded.append(deepcopy(pr_comments))
    runner = _FakeRunner(expanded)
    return (
        GitHubPullRequestProvider(
            resolver=lambda: "/trusted/bin/gh",
            runner=runner,
        ),
        runner,
    )


def _check_run(*, status: str = "COMPLETED", conclusion: str = "SUCCESS") -> dict[str, object]:
    """One check run, carrying only the fields the rollup selection asks for."""
    return {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "status": status,
        "conclusion": conclusion,
    }


class _CompletedRunner:
    def __init__(self, results: Sequence[subprocess.CompletedProcess[str]]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if any("comments(first:" in str(arg) for arg in argv):
            # The PR-level comment read is always issued after the thread read;
            # a hand-built runner that does not enumerate a response for it gets a
            # default empty page (digest ""), mirroring how _provider supplies one.
            # Uniquely identified by comments(first: -- the thread read selects no
            # comments connection and the rollup selects contexts(first:.
            return subprocess.CompletedProcess(
                list(argv), 0, stdout=json.dumps(_comments()), stderr=""
            )
        return self._results.pop(0)


def _completed(
    payload: dict[str, object] | None = None,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["gh"],
        returncode,
        stdout=json.dumps(payload) if payload is not None else "",
        stderr=stderr,
    )


def _core_and_rollup(**changes: object) -> tuple[dict[str, object], dict[str, object]]:
    """The primary and supplemental check responses for one subject, in order."""
    payload = _primary(**changes)
    return _envelope(_pr_node(payload)), _envelope(_rollup_node(payload))
