"""Reading a pull or merge request: parsing its URL, normalizing the provider's
payload, and the check rows, merge state and CI rollup it carries, in the full payload
and in the chip's status read (``_fetch_check_status``)."""

from __future__ import annotations

import pytest
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _rollup_reply

from kiro_crew.dashboard.handlers import source_providers as source


def test_parse_github_pull_request() -> None:
    ref = source.parse_source_url("https://github.com/kirodotdev/KiroCrew/pull/58?tab=checks")
    assert ref.provider == "github"
    assert ref.owner == "kirodotdev"
    assert ref.repo == "KiroCrew"  # brand-ok: the kirodotdev/KiroCrew repository name
    assert ref.number == 58
    assert ref.url == "https://github.com/kirodotdev/KiroCrew/pull/58"


def test_github_check_active_status_is_pending_even_with_success_conclusion() -> None:
    check = source._github_check({"name": "CI", "status": "IN_PROGRESS", "conclusion": "SUCCESS"})

    assert check["bucket"] == "pending"


def _run_row(
    name: str,
    *,
    run: int,
    conclusion: str,
    run_conclusion: str,
    status: str = "COMPLETED",
    definition: int = 1,
    event: str = "pull_request",
    workflow: str = "CI",
) -> dict:
    """One check-run row in the flat shape `_github_rollup_read` hands the collapse."""
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "workflowName": workflow,
        "workflowRunId": run,
        "workflowRunEvent": event,
        "workflowDefinitionId": definition,
        "workflowRunConclusion": run_conclusion,
    }


def test_github_checks_drop_a_row_its_cancelled_run_left_behind() -> None:
    """A concurrency-group cancellation whose replacement run passed must not
    roll up to `failed` -- the stale row is still genuinely in the payload."""
    rollup = [
        _run_row("Review", run=10, conclusion="CANCELLED", run_conclusion="CANCELLED"),
        _run_row("Review", run=11, conclusion="SUCCESS", run_conclusion="SUCCESS"),
    ]

    checks = source._github_checks(rollup)

    assert [check["bucket"] for check in checks] == ["passed"]
    assert source._rollup_ci([check["bucket"] for check in checks]) == "passed"


def test_github_checks_keep_a_failure_from_an_older_run() -> None:
    """A newer run id proves only that a run started later, not that it replaced
    the older one; an older run that FAILED is a live verdict and stays."""
    rollup = [
        _run_row("Review", run=10, conclusion="FAILURE", run_conclusion="FAILURE"),
        _run_row("Review", run=11, conclusion="SUCCESS", run_conclusion="SUCCESS"),
    ]

    checks = source._github_checks(rollup)

    assert [check["conclusion"] for check in checks] == ["FAILURE", "SUCCESS"]
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


@pytest.mark.parametrize("failure_first", [True, False])
def test_github_checks_keep_both_rows_of_one_run(failure_first: bool) -> None:
    """A workflow can publish a check run under its own job's display name, so
    two rows of ONE run are both live. Both arrival orders: under a last-wins
    rule one order passes for the wrong reason."""
    failure = _run_row("Fork PR Description", run=5, conclusion="FAILURE", run_conclusion="FAILURE")
    success = _run_row("Fork PR Description", run=5, conclusion="SUCCESS", run_conclusion="FAILURE")
    rollup = [failure, success] if failure_first else [success, failure]

    checks = source._github_checks(rollup)

    assert len(checks) == 2
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_do_not_group_two_workflow_files_sharing_one_name() -> None:
    """Two workflow files may carry one `name:`; the identity is the workflow
    DEFINITION, so one file's newer run cannot displace the other's rows."""
    rollup = [
        _run_row("Lint", run=10, conclusion="CANCELLED", run_conclusion="CANCELLED", definition=1),
        _run_row("Lint", run=11, conclusion="SUCCESS", run_conclusion="SUCCESS", definition=2),
    ]

    checks = source._github_checks(rollup)

    assert len(checks) == 2
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_do_not_group_runs_of_different_events() -> None:
    """One file on `push` and `pull_request` runs twice for one commit; those are
    concurrent dispatches, not an attempt and its replacement."""
    rollup = [
        _run_row("CI", run=10, conclusion="CANCELLED", run_conclusion="CANCELLED", event="push"),
        _run_row("CI", run=11, conclusion="SUCCESS", run_conclusion="SUCCESS"),
    ]

    assert len(source._github_checks(rollup)) == 2


def test_github_checks_keep_a_cancelled_row_of_a_live_run() -> None:
    """A row reaches CANCELLED inside a live run too (fail-fast, a failed
    `needs`); only a run that was itself cancelled proves displacement."""
    rollup = [
        _run_row("CI", run=10, conclusion="CANCELLED", run_conclusion="FAILURE"),
        _run_row("CI", run=11, conclusion="SUCCESS", run_conclusion="SUCCESS"),
    ]

    assert len(source._github_checks(rollup)) == 2


def test_github_checks_keep_a_cancelled_newest_run() -> None:
    """Dropping the newest run's cancellation would revive the verdict it replaced."""
    rollup = [
        _run_row("CI", run=10, conclusion="SUCCESS", run_conclusion="SUCCESS"),
        _run_row("CI", run=11, conclusion="CANCELLED", run_conclusion="CANCELLED"),
    ]

    assert [check["conclusion"] for check in source._github_checks(rollup)] == [
        "SUCCESS",
        "CANCELLED",
    ]


def test_github_checks_keep_rows_without_a_run_identity() -> None:
    """A row the host did not identify takes no part in the collapse."""
    rollup = [
        {"name": "Review", "workflowName": "Review", "status": "COMPLETED", "conclusion": c}
        for c in ("CANCELLED", "SUCCESS")
    ]

    assert len(source._github_checks(rollup)) == 2


def test_github_checks_queued_rerun_keeps_the_glyph_pending() -> None:
    """A queued re-run is live; the panel must not read green over it."""
    rollup = [
        _run_row("CI", run=10, conclusion="SUCCESS", run_conclusion="SUCCESS"),
        _run_row("CI", run=11, conclusion="", run_conclusion="", status="QUEUED"),
    ]

    checks = source._github_checks(rollup)

    assert source._rollup_ci([check["bucket"] for check in checks]) == "running"


def test_github_checks_do_not_collapse_across_publishers() -> None:
    """Identity is (workflow, name): collapsing on the display name alone would
    let one workflow's later success hide another's failure."""
    rollup = [
        {
            "name": "Lint",
            "workflowName": "Backend",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
        },
        {
            "name": "Lint",
            "workflowName": "Frontend",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T20:10:00Z",
        },
        # A legacy commit status has no workflow and keys on ("", context).
        {"context": "Lint", "state": "SUCCESS", "startedAt": "2026-07-28T20:20:00Z"},
    ]

    checks = source._github_checks(rollup)

    assert {(check["workflow"], check["bucket"]) for check in checks} == {
        ("Backend", "failed"),
        ("Frontend", "passed"),
        ("", "passed"),
    }
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_do_not_collapse_a_commit_status_into_a_check_run() -> None:
    """A legacy commit status and an app-published check-run both carry an empty
    workflow, so without the row kind in the identity one publisher's later
    success would hide the other's failure and roll the glyph up green."""
    rollup = [
        {
            "__typename": "CheckRun",
            "name": "CI",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
        },
        {
            "__typename": "StatusContext",
            "context": "CI",
            "state": "SUCCESS",
            "startedAt": "2026-07-28T20:30:00Z",
        },
    ]

    checks = source._github_checks(rollup)

    assert sorted(check["bucket"] for check in checks) == ["failed", "passed"]
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_classify_untyped_rows_by_shape_not_by_name() -> None:
    """A row without `__typename` must still be classified correctly. A status
    row carrying both `context` and `name` must not be read as a check-run and
    collide with a nameless check-run (both normalize to the `"Check"`
    placeholder), letting the status success hide the check-run failure."""
    rollup = [
        {
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
        },
        {
            "context": "Check",
            "name": "Check",
            "state": "SUCCESS",
            "startedAt": "2026-07-28T20:30:00Z",
        },
    ]

    checks = source._github_checks(rollup)

    assert sorted(check["bucket"] for check in checks) == ["failed", "passed"]
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_keep_matrix_legs_of_one_job_distinct() -> None:
    """GitHub appends matrix values to a check-run's name even when the workflow
    sets an explicit `name:`, so sibling shards of one job have distinct names.
    A failing shard must never be folded into a later-starting shard's success.
    """
    rollup = [
        {
            "__typename": "CheckRun",
            "name": "Backend Tests (3.10, 2)",
            "workflowName": "CI",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
        },
        {
            "__typename": "CheckRun",
            "name": "Backend Tests (3.12, 4)",
            "workflowName": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T20:10:00Z",
        },
    ]

    checks = source._github_checks(rollup)

    assert len(checks) == 2
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_never_collapse_workflowless_check_runs() -> None:
    """A check-run with no workflow comes from an app outside Actions, and the
    rollup carries no check-suite or run-attempt id to tell a superseded re-run
    from a same-named check by a different app. Leave such rows uncollapsed:
    over-counting is cosmetic, hiding a red behind another app's green is not."""
    rollup = [
        {
            "__typename": "CheckRun",
            "name": "security/scan",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-28T20:00:00Z",
            "detailsUrl": "https://app-one.example/run/1",
        },
        {
            "__typename": "CheckRun",
            "name": "security/scan",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-28T20:10:00Z",
            "detailsUrl": "https://app-two.example/run/9",
        },
    ]

    checks = source._github_checks(rollup)

    assert len(checks) == 2
    assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


def test_github_checks_preserve_input_order() -> None:
    """Collapsing removes rows; it never reshuffles the ones it keeps."""
    rollup = [
        _run_row("A", run=10, conclusion="CANCELLED", run_conclusion="CANCELLED"),
        _run_row("B", run=10, conclusion="CANCELLED", run_conclusion="CANCELLED"),
        _run_row("C", run=11, conclusion="SUCCESS", run_conclusion="SUCCESS"),
        _run_row("A", run=11, conclusion="SUCCESS", run_conclusion="SUCCESS"),
    ]

    assert [check["name"] for check in source._github_checks(rollup)] == ["B", "C", "A"]


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}, ("mergeable", "clean")),
        ({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}, ("conflicting", "dirty")),
        ({"mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"}, ("mergeable", "behind")),
        ({"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}, ("mergeable", "blocked")),
        ({"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}, ("unknown", "unknown")),
        ({}, ("", "")),
    ],
)
def test_github_merge_state_normalization(details: dict, expected: tuple[str, str]) -> None:
    assert source._github_merge_state(details) == expected


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"detailed_merge_status": "mergeable"}, ("mergeable", "clean")),
        ({"detailed_merge_status": "conflict"}, ("conflicting", "dirty")),
        ({"detailed_merge_status": "need_rebase"}, ("unknown", "need_rebase")),
        ({"detailed_merge_status": "not_approved"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "ci_must_pass"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "status_checks_must_pass"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "policies_denied"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "security_policy_violations"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "merge_request_blocked"}, ("unknown", "blocked")),
        ({"detailed_merge_status": "ci_still_running"}, ("unknown", "unstable")),
        ({"detailed_merge_status": "draft_status"}, ("unknown", "draft")),
        ({"detailed_merge_status": "checking"}, ("unknown", "unknown")),
        # Legacy merge_status is a fallback only when the detail is absent.
        ({"merge_status": "cannot_be_merged"}, ("conflicting", "")),
        ({"merge_status": "can_be_merged"}, ("mergeable", "")),
        # A stale legacy value must never override the authoritative detail:
        # not_approved + cannot_be_merged is blocked, NOT conflicting.
        (
            {"detailed_merge_status": "not_approved", "merge_status": "cannot_be_merged"},
            ("unknown", "blocked"),
        ),
        (
            {"detailed_merge_status": "mergeable", "merge_status": "cannot_be_merged"},
            ("mergeable", "clean"),
        ),
        (
            {"detailed_merge_status": "conflict", "merge_status": "can_be_merged"},
            ("conflicting", "dirty"),
        ),
        ({}, ("", "")),
    ],
)
def test_gitlab_merge_state_normalization(details: dict, expected: tuple[str, str]) -> None:
    assert source._gitlab_merge_state(details) == expected


def test_parse_gitlab_merge_request_with_nested_group() -> None:
    ref = source.parse_source_url("https://gitlab.com/acme/platform/service/-/merge_requests/42")
    assert ref.provider == "gitlab"
    assert ref.project == "acme/platform/service"
    assert ref.repo == "service"
    assert ref.number == 42


def _suite(run: int, run_conclusion: str, *, definition: int = 1, event: str = "pull_request"):
    """The check-suite edge GitHub nests a check run's workflow-run identity under."""
    return {
        "conclusion": run_conclusion,
        "workflowRun": {
            "databaseId": run,
            "event": event,
            "workflow": {"databaseId": definition, "name": "Review"},
        },
    }


@pytest.mark.asyncio
async def test_fetch_github_normalizes_commits_checks_comments_and_files(monkeypatch) -> None:
    limits: dict[str, int | None] = {}

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        limits[command] = kwargs.get("max_output_bytes")
        if "pr view" in command:
            return {
                "number": 12,
                "title": "Ship source tabs",
                "body": "## Summary\nAdds source tabs.",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "CONFLICTING",
                "mergeStateStatus": "DIRTY",
                "headRefName": "feature/source-tabs",
                "baseRefName": "main",
                "headRefOid": "abc123",
                "url": "https://github.com/acme/repo/pull/12",
                "author": {"login": "octocat"},
                "additions": 20,
                "deletions": 4,
                "changedFiles": 2,
                "commits": [
                    {
                        "oid": "abc123",
                        "messageHeadline": "Add source tabs",
                        "messageBody": "",
                        "authors": [{"login": "octocat"}],
                        "committedDate": "2026-07-13T12:00:00Z",
                    }
                ],
                "comments": [{"id": "c1", "author": {"login": "reviewer"}, "body": "Looks good"}],
                "reviews": [
                    {
                        "id": "r1",
                        "author": {"login": "reviewer"},
                        "body": "Approved",
                        "state": "APPROVED",
                    }
                ],
                "statusCheckRollup": [
                    {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
            }
        if "/files?" in command:
            return [
                {
                    "filename": "src/panel.tsx",
                    "status": "modified",
                    "additions": 20,
                    "deletions": 4,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]
        if "/comments?" in command:
            return [
                {
                    "id": 3,
                    "user": {"login": "inline-reviewer"},
                    "body": "Nit",
                    "path": "src/panel.tsx",
                    "line": 9,
                }
            ]
        if "statusCheckRollup" in command:
            return _rollup_reply([{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}])
        if "graphql" in command:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "PRRT_thread1",
                                        "isResolved": False,
                                        "comments": {"nodes": [{"databaseId": 3}]},
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["provider"] == "github"
    # The plugin contract (`SourceChangePayload`) is defined as "what the
    # built-in fetchers produce", so the two must not drift: a key added to or
    # removed from `_fetch_github` without the schema (or vice versa) breaks
    # every downstream plugin silently. Exact equality, both directions (the
    # GitHub fetcher emits none of the `total=False` extras).
    assert set(data) == set(source.SourceChangePayload.__required_keys__)
    assert set(data["commits"][0]) == set(source.SourceChangeCommit.__annotations__)
    assert set(data["files"][0]) == set(source.SourceChangeFile.__annotations__)
    assert {frozenset(comment) for comment in data["comments"]} == {
        frozenset(source.SourceChangeComment.__annotations__)
    }
    assert data["mergeable"] == "conflicting"
    assert data["mergeStateStatus"] == "dirty"
    assert data["commits"][0]["sha"] == "abc123"
    assert data["checks"][0]["bucket"] == "passed"
    assert {comment["kind"] for comment in data["comments"]} == {"comment", "review", "inline"}
    assert data["files"][0]["patch"].startswith("@@")
    assert data["partialSections"] == ["files"]

    inline = next(comment for comment in data["comments"] if comment["kind"] == "inline")
    assert inline["threadId"] == "PRRT_thread1"
    assert inline["resolvable"] is True
    assert inline["resolved"] is False
    top_level = next(comment for comment in data["comments"] if comment["kind"] == "comment")
    assert top_level["threadId"] == ""
    assert top_level["resolvable"] is False
    assert next(limit for command, limit in limits.items() if "pr view" in command) is None
    assert (
        next(limit for command, limit in limits.items() if "/files?" in command)
        == source._DIFF_OUTPUT_BYTES
    )
    assert (
        next(limit for command, limit in limits.items() if "/comments?" in command)
        == source._DISCUSSION_OUTPUT_BYTES
    )
    assert (
        next(limit for command, limit in limits.items() if "reviewThreads" in command)
        == source._DISCUSSION_OUTPUT_BYTES
    )
    assert (
        next(limit for command, limit in limits.items() if "statusCheckRollup" in command)
        == source._CHECKS_OUTPUT_BYTES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_endpoint", "expected_section"),
    [
        ("files", "files"),
        ("comments", "inline review comments"),
        ("threads", "inline review comments"),
    ],
)
async def test_fetch_github_marks_failed_secondary_endpoints_partial(
    monkeypatch, failed_endpoint: str, expected_section: str
) -> None:
    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "pr view" in command:
            return {"number": 12, "changedFiles": 0}
        should_fail = (
            (failed_endpoint == "files" and "/files?" in command)
            or (failed_endpoint == "comments" and "/comments?" in command)
            or (failed_endpoint == "threads" and "reviewThreads" in command)
        )
        if should_fail:
            raise source.SourceProviderError("secondary request failed")
        if "statusCheckRollup" in command:
            return _rollup_reply([])
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["partialSections"] == [expected_section]


@pytest.mark.asyncio
async def test_fetch_github_reads_rollup_outside_the_core_field_set(monkeypatch) -> None:
    """The core `pr view` field set must not bundle `statusCheckRollup`.

    `gh` resolves a `--json` field set atomically, so a bundled rollup made a
    fine-grained token without Checks read access fail the WHOLE panel read.
    """
    commands: list[tuple[str, int | None]] = []

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        commands.append((command, kwargs.get("max_output_bytes")))
        if "statusCheckRollup" in command:
            return _rollup_reply([{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}])
        if "pr view" in command:
            return {"number": 12, "title": "Split", "state": "OPEN", "headRefOid": "abc123"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    core_reads = [
        command for command, _limit in commands if "pr view" in command and "title" in command
    ]
    assert core_reads
    assert all("statusCheckRollup" not in command for command in core_reads)
    rollup_reads = [
        (command, limit) for command, limit in commands if "statusCheckRollup" in command
    ]
    assert len(rollup_reads) == 1
    assert rollup_reads[0][0].startswith("gh api graphql -f query=")
    assert rollup_reads[0][0].endswith("-f o=acme -f r=repo -F n=12")
    assert rollup_reads[0][1] == source._CHECKS_OUTPUT_BYTES
    assert data["checks"][0]["bucket"] == "passed"
    assert "checks" not in data["partialSections"]


@pytest.mark.asyncio
async def test_fetch_github_core_payload_survives_rollup_failure(monkeypatch) -> None:
    """A Checks-blind token costs the checks SECTION, never the panel."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            raise source.SourceProviderError("gh: Resource not accessible by integration")
        if "pr view" in command:
            return {
                "number": 12,
                "title": "Fine-grained token",
                "state": "OPEN",
                "headRefOid": "abc123",
            }
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["title"] == "Fine-grained token"
    assert data["state"] == "OPEN"
    assert data["checks"] == []
    # The degraded state is distinguishable IN THE PAYLOAD: an empty `checks`
    # list plus the named partial section, never a silent "no checks".
    assert "checks" in data["partialSections"]


@pytest.mark.asyncio
async def test_fetch_github_discards_rollup_from_a_different_head(monkeypatch) -> None:
    """A rollup read that straddled a push must not pin another commit's CI."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            return _rollup_reply(
                [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                head="pushed-after-core-read",
            )
        if "pr view" in command:
            return {"number": 12, "state": "OPEN", "headRefOid": "abc123"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["checks"] == []
    assert "checks" in data["partialSections"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_endpoint", "expected_section"),
    [
        ("commits", "commits"),
        ("discussions", "review discussions"),
        ("changes", "files"),
        ("pipelines", "checks"),
        ("jobs", "checks"),
    ],
)
async def test_fetch_gitlab_marks_failed_secondary_endpoints_partial(
    monkeypatch, failed_endpoint: str, expected_section: str
) -> None:
    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {"iid": 42, "changes_count": "0"}
        should_fail = (
            (failed_endpoint == "commits" and "/commits?" in command)
            or (failed_endpoint == "discussions" and "/discussions?" in command)
            or (failed_endpoint == "changes" and command.endswith("/changes"))
            or (failed_endpoint == "pipelines" and "/pipelines?" in command)
            or (failed_endpoint == "jobs" and "/jobs?" in command)
        )
        if should_fail:
            raise source.SourceProviderError("secondary request failed")
        if "/pipelines?" in command:
            return [{"id": 91}] if failed_endpoint == "jobs" else []
        if command.endswith("/changes"):
            return {"changes": []}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert data["partialSections"] == [expected_section]


MERGE_STATE_REREAD_FIELDS = "mergeable,mergeStateStatus"


@pytest.mark.asyncio
async def test_fetch_github_rereads_merge_state_until_the_provider_settles_it(
    monkeypatch,
) -> None:
    """GitHub computes mergeability lazily: the first read says UNKNOWN.

    Without the re-read the panel reports no merge blocker at all on first open,
    and the conflict only surfaces once the user hits refresh.
    """
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    rereads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if MERGE_STATE_REREAD_FIELDS in command:
            rereads.append(command)
            if len(rereads) == 1:
                return {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
            return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
        if "pr view" in command:
            return {"number": 12, "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert (data["mergeable"], data["mergeStateStatus"]) == ("conflicting", "dirty")
    # The re-read asks for the merge fields alone, not another full fanout.
    assert len(rereads) == 2
    assert all("statusCheckRollup" not in command for command in rereads)


@pytest.mark.asyncio
async def test_fetch_github_does_not_reread_settled_merge_state(monkeypatch) -> None:
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    rereads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if MERGE_STATE_REREAD_FIELDS in command:
            rereads.append(command)
            return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
        if "pr view" in command:
            return {"number": 12, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert (data["mergeable"], data["mergeStateStatus"]) == ("mergeable", "clean")
    assert rereads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unsettled", "provider_error", "invalid_payload"])
async def test_fetch_github_degrades_to_unknown_when_reread_cannot_settle(
    monkeypatch, outcome: str
) -> None:
    """A merge state that stays unknown degrades one banner, never the panel."""
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    rereads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if MERGE_STATE_REREAD_FIELDS in command:
            rereads.append(command)
            if outcome == "provider_error":
                raise source.SourceProviderError("merge state read failed")
            if outcome == "invalid_payload":
                return []
            return {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
        if "pr view" in command:
            return {"number": 12, "title": "Still checking", "mergeable": "UNKNOWN"}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["mergeable"] == "unknown"
    assert data["title"] == "Still checking"
    # A failed or invalid re-read stops immediately; an unsettled one uses the
    # whole bounded budget and no more.
    assert len(rereads) == (source._MERGE_STATE_REREADS if outcome == "unsettled" else 1)


@pytest.mark.asyncio
async def test_fetch_github_skips_reread_when_provider_omits_merge_fields(monkeypatch) -> None:
    """An absent field is not "still computing" — re-reading it would never settle."""
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    rereads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if MERGE_STATE_REREAD_FIELDS in command:
            rereads.append(command)
            return {}
        if "pr view" in command:
            return {"number": 12}
        return {} if "graphql" in command else []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_github(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert data["mergeable"] == ""
    assert rereads == []


@pytest.mark.asyncio
async def test_fetch_gitlab_rereads_merge_state_until_the_provider_settles_it(
    monkeypatch,
) -> None:
    """GitLab reports ``checking``/``unchecked`` while it evaluates the MR."""
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    detail_reads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            detail_reads.append(command)
            if len(detail_reads) == 1:
                return {"iid": 42, "detailed_merge_status": "checking"}
            return {"iid": 42, "detailed_merge_status": "conflict"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert (data["mergeable"], data["mergeStateStatus"]) == ("conflicting", "dirty")
    assert len(detail_reads) == 2


@pytest.mark.asyncio
async def test_fetch_gitlab_merge_state_reread_passes_the_host(monkeypatch) -> None:
    """The re-read is a ``glab`` call, and ``_run_provider`` refuses any ``glab``
    call without a host. A re-read that drops it is denied, the loop swallows
    the refusal and the panel shows ``unknown`` for an MR GitLab had settled."""
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    detail_hosts: list[str] = []

    async def fake_run(*argv: str, host: str = "", **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            detail_hosts.append(host)
            if len(detail_hosts) == 1:
                return {"iid": 42, "detailed_merge_status": "checking"}
            return {"iid": 42, "detailed_merge_status": "mergeable"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert detail_hosts == ["gitlab.com", "gitlab.com"]
    assert (data["mergeable"], data["mergeStateStatus"]) == ("mergeable", "clean")


@pytest.mark.asyncio
async def test_fetch_gitlab_degrades_to_unknown_when_reread_cannot_settle(monkeypatch) -> None:
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    detail_reads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            detail_reads.append(command)
            return {"iid": 42, "title": "Still checking", "detailed_merge_status": "unchecked"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert data["mergeable"] == "unknown"
    assert data["title"] == "Still checking"
    assert len(detail_reads) == 1 + source._MERGE_STATE_REREADS


@pytest.mark.asyncio
async def test_fetch_github_checks_collapses_superseded_runs(monkeypatch) -> None:
    """The panel polls this endpoint while checks are pending and writes its
    reply over the full payload's `checks`, so an uncollapsed reply would
    resurrect the inflated counts and the superseded failure it just fixed."""

    async def fake_run(*_argv: str, **_kwargs: int):
        return _rollup_reply(
            [
                {
                    "name": "Review",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "checkSuite": _suite(10, "CANCELLED"),
                },
                {
                    "name": "Review",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "checkSuite": _suite(11, "SUCCESS"),
                },
            ]
        )

    monkeypatch.setattr(source, "_run_json", fake_run)

    checks = await source._fetch_github_checks(
        source.parse_source_url("https://github.com/acme/repo/pull/12")
    )

    assert [(check["workflow"], check["bucket"]) for check in checks] == [("Review", "passed")]


@pytest.mark.asyncio
async def test_fetch_github_checks_keeps_both_rows_of_one_run(monkeypatch) -> None:
    """Measured shape: a workflow publishing a check run under its own job's
    display name leaves two rows of ONE run, SUCCESS and FAILURE. The read must
    keep both whichever arrives first, or the glyph reads green over red."""
    failure = {
        "name": "Fork PR Description",
        "status": "COMPLETED",
        "conclusion": "FAILURE",
        "checkSuite": _suite(7, "FAILURE"),
    }
    success = {**failure, "conclusion": "SUCCESS"}
    for rows in ([failure, success], [success, failure]):

        async def fake_run(*_argv: str, _rows: list = rows, **_kwargs: int):
            return _rollup_reply(_rows)

        monkeypatch.setattr(source, "_run_json", fake_run)

        checks = await source._fetch_github_checks(
            source.parse_source_url("https://github.com/acme/repo/pull/12")
        )

        assert source._rollup_ci([check["bucket"] for check in checks]) == "failed"


@pytest.mark.asyncio
async def test_github_rollup_read_follows_pages_and_refuses_a_moved_head(monkeypatch) -> None:
    """Pages are joined before the collapse (a successor can sit on a later page),
    and a page naming another head discards the whole read."""
    first = _rollup_reply(
        [
            {
                "name": "Review",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "checkSuite": _suite(10, "CANCELLED"),
            }
        ]
    )
    contexts = first["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
        "statusCheckRollup"
    ]["contexts"]
    contexts["pageInfo"] = {"hasNextPage": True, "endCursor": "CUR1"}
    second_rows = [
        {
            "name": "Review",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "checkSuite": _suite(11, "SUCCESS"),
        }
    ]
    commands: list[str] = []
    second_head = "abc123"

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        commands.append(command)
        return _rollup_reply(second_rows, head=second_head) if "c=CUR1" in command else first

    monkeypatch.setattr(source, "_run_json", fake_run)
    ref = source.parse_source_url("https://github.com/acme/repo/pull/12")

    checks, head = await source._github_rollup_read(ref)

    assert head == "abc123"
    assert len(commands) == 2
    assert [check["bucket"] for check in checks] == ["passed"]

    second_head = "pushed-mid-read"
    with pytest.raises(source.SourceProviderError):
        await source._github_rollup_read(ref)


@pytest.mark.asyncio
async def test_github_check_status_collapses_superseded_runs(monkeypatch) -> None:
    """The chip glyph reads the same collapse the panel does, so a superseded
    CANCELLED row cannot leave the sidebar red."""

    async def fake_run(*argv: str, **_kwargs: int):
        if "statusCheckRollup" in " ".join(argv):
            return _rollup_reply(
                [
                    {
                        "name": "Review",
                        "status": "COMPLETED",
                        "conclusion": "CANCELLED",
                        "checkSuite": _suite(10, "CANCELLED"),
                    },
                    {
                        "name": "Review",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "checkSuite": _suite(11, "SUCCESS"),
                    },
                ]
            )
        return {"state": "OPEN"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status == {"state": "open", "ci": "passed"}


@pytest.mark.asyncio
async def test_github_check_status_carries_settled_merge_state(monkeypatch) -> None:
    """The chip cache carries merge state so a conflict that appears while the
    panel is open lands on a poll instead of waiting for a manual refresh."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            return _rollup_reply([])
        assert "mergeable,mergeStateStatus" in command
        return {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status == {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_mergeable", ["UNKNOWN", None])
async def test_github_check_status_omits_unsettled_merge_state(
    monkeypatch, raw_mergeable: str | None
) -> None:
    """"Still computing" must not overwrite the answer the full payload has."""  # fmt: skip

    async def fake_run(*argv: str, **_kwargs: int):
        if "statusCheckRollup" in " ".join(argv):
            return _rollup_reply([])
        return {"state": "OPEN", "mergeable": raw_mergeable, "mergeStateStatus": "UNKNOWN"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status == {"state": "open"}


@pytest.mark.asyncio
async def test_github_check_status_keeps_authorized_fields_when_rollup_fails(
    monkeypatch,
) -> None:
    """The chip renders state/merge under a Checks-blind token.

    Bundling `statusCheckRollup` into the chip read made the WHOLE read fail
    when the token lacked Checks access; the split keeps the fields the token
    was authorized for and flags only the CI portion as unavailable.
    """

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            raise source.SourceProviderError("gh: Resource not accessible by integration")
        return {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status is not None
    assert status.pop(source._CHIP_CI_UNAVAILABLE, None) == "1"
    assert status == {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"}


@pytest.mark.asyncio
async def test_github_check_status_marks_stale_rollup_unavailable(monkeypatch) -> None:
    """A rollup read that straddled a push must not paint another head's CI."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            return _rollup_reply(
                [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                head="pushed-after-core-read",
            )
        return {"state": "OPEN", "headRefOid": "abc123"}

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://github.com/acme/repo/pull/12")

    assert status is not None
    assert status.pop(source._CHIP_CI_UNAVAILABLE, None) == "1"
    assert status == {"state": "open"}


@pytest.mark.asyncio
async def test_github_check_status_still_fails_when_core_read_fails(monkeypatch) -> None:
    """Only the rollup is degradable: a failed CORE read must keep raising, so
    `_refresh_check_status` keeps its keep-previous-wholesale posture."""

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if "statusCheckRollup" in command:
            return _rollup_reply([])
        raise source.SourceProviderError("core read failed")

    monkeypatch.setattr(source, "_run_json", fake_run)

    with pytest.raises(source.SourceProviderError):
        await source._fetch_check_status("https://github.com/acme/repo/pull/12")


@pytest.mark.asyncio
async def test_gitlab_check_status_carries_settled_merge_state(monkeypatch) -> None:
    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {"state": "opened", "detailed_merge_status": "conflict"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://gitlab.com/acme/repo/-/merge_requests/42")

    assert status == {"state": "open", "mergeable": "conflicting", "mergeStateStatus": "dirty"}


@pytest.mark.asyncio
async def test_fetch_gitlab_treats_a_detail_only_answer_as_settled(monkeypatch) -> None:
    """GitLab settles need_rebase with ``mergeable`` still ``unknown``.

    Keying settledness on ``mergeable`` alone re-read a state the provider had
    already answered, then threw the answer away.
    """
    monkeypatch.setattr(source, "_MERGE_STATE_REREAD_DELAY_SECS", 0)
    detail_reads: list[str] = []

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            detail_reads.append(command)
            return {"iid": 42, "detailed_merge_status": "need_rebase"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    data = await source._fetch_gitlab(
        source.parse_source_url("https://gitlab.com/acme/repo/-/merge_requests/42")
    )

    assert (data["mergeable"], data["mergeStateStatus"]) == ("unknown", "need_rebase")
    assert len(detail_reads) == 1


@pytest.mark.parametrize(
    ("pair", "settled"),
    [
        (("conflicting", "dirty"), True),
        (("mergeable", "clean"), True),
        # GitLab: the detail is the answer, mergeable never settles.
        (("unknown", "need_rebase"), True),
        (("unknown", "blocked"), True),
        # Nothing answered yet -> a re-read may settle it.
        (("unknown", "unknown"), False),
        (("unknown", ""), False),
        # Provider omitted the fields -> re-reading cannot settle them.
        (("", ""), True),
    ],
)
def test_merge_state_settled_considers_both_fields(pair: tuple[str, str], settled: bool) -> None:
    assert source._merge_state_settled(*pair) is settled


@pytest.mark.asyncio
async def test_gitlab_check_status_carries_a_detail_only_answer(monkeypatch) -> None:
    """The rebase/blocked banners are driven by the detail field alone.

    Dropping it because ``mergeable`` is unknown left exactly those banners
    invisible to the status poll.
    """

    async def fake_run(*argv: str, **_kwargs: int):
        command = " ".join(argv)
        if command.endswith("merge_requests/42"):
            return {"state": "opened", "detailed_merge_status": "need_rebase"}
        return []

    monkeypatch.setattr(source, "_run_json", fake_run)

    status = await source._fetch_check_status("https://gitlab.com/acme/repo/-/merge_requests/42")

    assert status == {"state": "open", "mergeStateStatus": "need_rebase"}


def test_status_from_full_payload_projects_the_merge_pair() -> None:
    """The write-through must carry the merge pair the chip read records.

    If it dropped the pair, every full fetch would rewrite the chip entry without
    it, the next chip refresh would judge that a change and drop the full payload,
    and the write-through would strip it again — the repeating chip↔full
    transition the flap damper exists to contain, spun by a projection gap.
    """
    projected = source.status_from_full_payload(
        {
            "state": "OPEN",
            "draft": False,
            "checks": [{"bucket": "passed"}],
            "mergeable": "conflicting",
            "mergeStateStatus": "dirty",
        }
    )

    assert projected == {
        "ci": "passed",
        "state": "open",
        "mergeable": "conflicting",
        "mergeStateStatus": "dirty",
    }


def test_full_payload_and_chip_projections_agree_on_the_merge_pair() -> None:
    """Both surfaces must derive the identical pair, or they flap against each other."""
    chip: dict[str, str] = {}
    source._record_merge_state(chip, "unknown", "need_rebase")
    projected = source.status_from_full_payload(
        {"state": "opened", "mergeable": "unknown", "mergeStateStatus": "need_rebase"}
    )

    assert chip == {"mergeStateStatus": "need_rebase"}
    assert projected is not None
    assert {key: value for key, value in projected.items() if key.startswith("merge")} == chip
