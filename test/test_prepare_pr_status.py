"""Regression tests for the kirocrew-prepare-pr aggregate readiness policy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import ModuleType

from prepare_pr_status_helpers import (
    _fake_git,
    _fake_git_with,
    _install_fake_gh,
    _last_line_json,
    _load_script,
    _pr_payload,
)
from skill_script_helpers import is_rollup_graphql_read, rollup_graphql_response


def test_json_flag_does_not_change_the_exit_code(capsys) -> None:
    clean = _pr_payload([{"context": "PR Readiness", "state": "SUCCESS"}])
    blocked = _pr_payload([{"name": "CI", "status": "COMPLETED", "conclusion": "FAILURE"}])

    for payload, expected in ((clean, 0), (blocked, 20)):
        module = _load_script()
        _install_fake_gh(module, payload)
        assert module.main(["pr_status.py", "42"]) == expected
        capsys.readouterr()

        module = _load_script()
        _install_fake_gh(module, payload)
        assert module.main(["pr_status.py", "42", "--json"]) == expected
        assert _last_line_json(capsys)["exit_code"] == expected


def test_json_report_carries_the_full_head_sha_not_the_truncated_prose_one(capsys) -> None:
    module = _load_script()
    _install_fake_gh(module, _pr_payload([{"context": "PR Readiness", "state": "SUCCESS"}]))

    module.main(["pr_status.py", "42", "--json"])

    head = _last_line_json(capsys)["progress_key"]["head_sha"]
    assert head == "f" * 40
    assert len(head) == 40


def test_bare_json_flag_is_not_read_as_the_pr_number() -> None:
    """A boolean flag left in the positional list would resolve the wrong PR."""
    module = _load_script()
    payload = _pr_payload([{"context": "PR Readiness", "state": "SUCCESS"}])
    seen: list[list[str]] = []

    def fake_run(args: list[str]) -> tuple[int, str, str]:
        seen.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:5] == ["gh", "pr", "view", "--json", "number"]:
            return 0, "42", ""
        if args[:3] == ["gh", "pr", "view"]:
            return 0, payload, ""
        if args[:3] == ["gh", "repo", "view"]:
            return 0, "example/repo", ""
        if is_rollup_graphql_read(args):
            core = json.loads(payload)
            return 0, rollup_graphql_response(core["statusCheckRollup"], core["headRefOid"]), ""
        if args[:2] == ["gh", "api"] and "/issues/" in args[2] and "/comments" in args[2]:
            return 0, "[]", ""
        if args[:2] == ["gh", "api"] and "/actions/runs" in args[2]:
            return 0, json.dumps({"total_count": 1, "workflow_runs": [{"event": "pull_request"}]}), ""
        raise AssertionError("unexpected command: {}".format(args))

    module.run = fake_run
    module.unresolved_thread_count = lambda _number: 0

    assert module.main(["pr_status.py", "--json"]) == 0

    # The auto-detect branch must have run, i.e. --json was NOT taken as the PR.
    assert ["gh", "pr", "view", "--json", "number", "-q", ".number"] in seen
    detail = [c for c in seen if c[:3] == ["gh", "pr", "view"] and c[3:4] not in ([], ["--json"])]
    assert detail and detail[0][3] == "42"


def test_progress_key_is_identical_for_an_unchanged_pr(capsys) -> None:
    payload = _pr_payload([{"name": "CI", "status": "COMPLETED", "conclusion": "FAILURE"}])

    keys = []
    for _ in range(2):
        module = _load_script()
        _install_fake_gh(module, payload)
        module.main(["pr_status.py", "42", "--json"])
        keys.append(json.dumps(_last_line_json(capsys)["progress_key"], sort_keys=True))

    assert keys[0] == keys[1]


def test_progress_key_changes_when_the_head_moves(capsys) -> None:
    checks = [{"name": "CI", "status": "COMPLETED", "conclusion": "FAILURE"}]

    module = _load_script()
    _install_fake_gh(module, _pr_payload(checks))
    module.main(["pr_status.py", "42", "--json"])
    before = _last_line_json(capsys)["progress_key"]

    module = _load_script()
    _install_fake_gh(module, _pr_payload(checks, headRefOid="a" * 40))
    module.main(["pr_status.py", "42", "--json"])
    after = _last_line_json(capsys)["progress_key"]

    assert before != after
    assert after["head_sha"] == "a" * 40


def test_progress_key_ignores_the_unresolved_thread_count(capsys) -> None:
    """A thread count degrades to null on an API blip; it must not read as progress."""
    payload = _pr_payload([{"name": "CI", "status": "COMPLETED", "conclusion": "FAILURE"}])

    module = _load_script()
    _install_fake_gh(module, payload)
    module.unresolved_thread_count = lambda _number: 3
    module.main(["pr_status.py", "42", "--json"])
    first = _last_line_json(capsys)

    module = _load_script()
    _install_fake_gh(module, payload)
    module.unresolved_thread_count = lambda _number: None
    module.main(["pr_status.py", "42", "--json"])
    second = _last_line_json(capsys)

    assert first["progress_key"] == second["progress_key"]
    assert first["advisory"]["unresolved_threads"] == 3
    assert second["advisory"]["unresolved_threads"] is None


def test_failing_checks_are_listed_sorted_and_exclude_passing_ones(capsys) -> None:
    module = _load_script()
    _install_fake_gh(
        module,
        _pr_payload(
            [
                {"name": "zeta lint", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "alpha tests", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "passing build", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]
        ),
    )

    module.main(["pr_status.py", "42", "--json"])

    key = _last_line_json(capsys)["progress_key"]
    assert key["failing_checks"] == ["alpha tests", "zeta lint"]
    assert key["checks_failing"] == 2


def test_same_check_name_in_two_workflows_does_not_collide_in_the_key(capsys) -> None:
    """A failing check's identity must carry its workflow, not just its name.

    Two workflows can publish the same check name. If one workflow's copy starts
    failing while the other's stops, a name-only list is byte-identical across
    that change and a stall streak would run through a PR whose blocking check
    actually moved.
    """
    def payload(ci_fails: bool) -> str:
        return _pr_payload(
            [
                {
                    "name": "Tests",
                    "workflowName": "CI",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE" if ci_fails else "SUCCESS",
                },
                {
                    "name": "Tests",
                    "workflowName": "Nightly",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS" if ci_fails else "FAILURE",
                },
            ]
        )

    keys = []
    for ci_fails in (True, False):
        module = _load_script()
        _install_fake_gh(module, payload(ci_fails))
        assert module.main(["pr_status.py", "42", "--json"]) == 20
        keys.append(_last_line_json(capsys)["progress_key"])

    assert keys[0]["failing_checks"] == ["CI / Tests"]
    assert keys[1]["failing_checks"] == ["Nightly / Tests"]
    assert keys[0] != keys[1]
    # The rest of the key is identical, so the workflow qualifier is the only
    # thing distinguishing these two states -- strip it and they collide.
    assert {k: v for k, v in keys[0].items() if k != "failing_checks"} == {
        k: v for k, v in keys[1].items() if k != "failing_checks"
    }


def test_a_status_context_keeps_its_bare_context_name(capsys) -> None:
    """StatusContexts have no workflow; their context name IS the identity."""
    module = _load_script()
    _install_fake_gh(module, _pr_payload([{"context": "legacy/build", "state": "FAILURE"}]))

    module.main(["pr_status.py", "42", "--json"])

    assert _last_line_json(capsys)["progress_key"]["failing_checks"] == ["legacy/build"]


def test_a_changed_blocker_changes_the_key_even_with_an_identical_check_set(capsys) -> None:
    """A different reason for being blocked must reset a stall streak, not extend it.

    exit_code and the failing-check set cannot tell "blocked by a failing check"
    from "blocked by a merge conflict": both are exit 20 and here carry a
    byte-identical check set, head and readiness. Only the verdict reason
    distinguishes them, which is why `status` is part of the key.
    """
    checks = [{"name": "CI", "status": "COMPLETED", "conclusion": "FAILURE"}]

    module = _load_script()
    _install_fake_gh(module, _pr_payload(checks))
    assert module.main(["pr_status.py", "42", "--json"]) == 20
    failing_check = _last_line_json(capsys)["progress_key"]

    module = _load_script()
    _install_fake_gh(
        module,
        _pr_payload(checks, mergeable="CONFLICTING", mergeStateStatus="DIRTY"),
    )
    assert module.main(["pr_status.py", "42", "--json"]) == 20
    conflicted = _last_line_json(capsys)["progress_key"]

    assert failing_check != conflicted
    # And prove `status` is what discriminates: strip it and they collide, which
    # is the false stall-streak completion this field exists to prevent.
    assert {k: v for k, v in failing_check.items() if k != "status"} == {
        k: v for k, v in conflicted.items() if k != "status"
    }


def test_report_emits_only_the_consumed_surface(capsys) -> None:
    """Every emitted field has a named consumer in the babysit skill.

    Pins the absence of ambient PR state (mergeable / merge_state /
    review_decision / check totals / head_run): the prose above already prints
    it and the skill reads it there, so a second machine-readable copy with no
    reader would be a surface to keep in sync for nothing.
    """
    module = _load_script()
    _install_fake_gh(module, _pr_payload([{"context": "PR Readiness", "state": "SUCCESS"}]))

    module.main(["pr_status.py", "42", "--json"])
    report = _last_line_json(capsys)

    assert set(report) == {"exit_code", "pr", "status", "url", "progress_key", "advisory"}
    assert set(report["progress_key"]) == {
        "checks_failing",
        "exit_code",
        "failing_checks",
        "head_sha",
        "readiness_kind",
        "status",
    }
    assert set(report["advisory"]) == {
        "blocking_reviewers",
        "bot_comments_readable",
        "elided_stamp_reviewers",
        "findings",
        "green_age",
        "overridden_reviewers",
        "stale_reviewers",
        # Consumer: the readiness gate reads `blocking_dropped` to block a merge,
        # and the babysit loop reads that plus `readable`, so an UNKNOWN reading is
        # not mistaken for "none found".
        "superseded_verdicts",
        "unresolved_threads",
    }


def test_the_green_age_line_qualifies_the_rollup_without_gating_it(capsys) -> None:
    """A green is a verdict about one base commit; the line says which.

    Printed beside the rollup because it qualifies the rollup, and read from the
    same JSON object the poll loop already parses.
    """
    module = _load_script()
    _install_fake_gh(module, _pr_payload([{"context": "PR Readiness", "state": "SUCCESS"}]))

    code = module.main(["pr_status.py", "42", "--json"])
    out = capsys.readouterr().out
    report = json.loads([ln for ln in out.strip().splitlines() if ln.strip()][-1])

    assert code == 0
    assert "green age: base +0 commits" in out
    assert "overlap: none" in out
    assert report["advisory"]["green_age"]["stale"] is False
    # Advisory only: never in the key a stall tripwire compares.
    assert "green_age" not in report["progress_key"]


def test_a_stale_green_is_reported_and_changes_no_exit_code(capsys) -> None:
    """THE WHOLE POINT: information for the merger, never a gate.

    The base moved in a file this PR also owns, so the green describes a tree
    that will not merge -- and the tool still exits 0, because turning this
    into a gate would put a client-side heuristic in front of every merge on a
    repository whose merge gap is measured in minutes.
    """
    module = _load_script()
    _install_fake_gh(
        module,
        _pr_payload([{"context": "PR Readiness", "state": "SUCCESS"}]),
        git=lambda args: _fake_git_with(
            args, moved=["src/kiro_crew/ledger/store.py"], mine=[], commits=3
        ),
        pr_files=["src/kiro_crew/ledger/store.py"],
    )

    code = module.main(["pr_status.py", "42", "--json"])
    out = capsys.readouterr().out
    report = json.loads([ln for ln in out.strip().splitlines() if ln.strip()][-1])

    assert code == 0, "the green-age line is information, not a gate"
    assert "src/kiro_crew/ledger/store.py (same-file)" in out
    assert report["advisory"]["green_age"]["stale"] is True
    assert report["advisory"]["green_age"]["commits"] == 3


def test_a_probe_that_cannot_measure_says_unavailable(capsys) -> None:
    """Unknown reads as unknown, in both directions.

    A probe that cannot answer must not report a fresh green, and must not turn a
    readable PR into an error either.
    """
    module = _load_script()

    def exploding_git(args: list[str]) -> tuple[int, str, str]:
        raise RuntimeError("git is not installed on this host")

    _install_fake_gh(
        module,
        _pr_payload([{"context": "PR Readiness", "state": "SUCCESS"}]),
        git=exploding_git,
    )

    code = module.main(["pr_status.py", "42", "--json"])
    out = capsys.readouterr().out
    report = json.loads([ln for ln in out.strip().splitlines() if ln.strip()][-1])

    assert code == 0
    assert "green age: unavailable" in out
    assert "FRESH" not in out
    assert report["advisory"]["green_age"]["ok"] is False


def test_the_probe_is_asked_about_the_hosts_own_base_branch() -> None:
    """A PR against a release branch is measured against THAT branch."""
    module = _load_script()
    seen: dict[str, object] = {}
    _install_fake_gh(module, _pr_payload([], baseRefName="release/0.7"))
    real = module.probe_green_age

    def spy(base, head_sha, pr):
        seen.update({"base": base, "head": head_sha, "pr": pr})
        return real(base, head_sha, pr)

    module.probe_green_age = spy
    module.main(["pr_status.py", "42"])

    assert seen["base"] == "release/0.7"
    assert seen["head"] == "f" * 40


def test_passed_aggregate_does_not_clear_an_observed_failing_row() -> None:
    """A green aggregate must not suppress an observed failing row.

    The aggregate's context name is a forgeable display string, so letting its
    green erase a failing row would let a forged green flip the tool to CLEAN
    over a real failure. An observed failure is authoritative: the failing row
    survives the passed aggregate and the tool blocks.
    """
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "Backend Tests", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"context": "PR Readiness", "state": "SUCCESS"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_failing_aggregate_still_fails() -> None:
    """A failing aggregate over no failing row is action required, not clean."""
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "Backend Tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "PR Readiness", "state": "FAILURE"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_passed_aggregate_does_not_conclude_over_a_still_running_lane() -> None:
    """A green aggregate must not conclude the round while a real lane runs.

    The aggregate's context name is forgeable, so if a passed aggregate could
    conclude the "still running" gate, a forged green posted while a real lane
    is still IN_PROGRESS would skip it and reach CLEAN before the real failure
    lands -- the forged-green-to-CLEAN vector moved into a timing window. An
    observed running row keeps the round open on its own terms: RUNNING, not
    CLEAN.

    This reverses the inverted assertion below on purpose (recorded in the PR
    description): the running gate does not defer to a passed aggregate, on the
    same rule that governs the failing gate -- the forgeable aggregate subtracts
    no observed row. The chosen cost is a genuinely stuck orphaned running row
    holding the tool at RUNNING (exit 10, visible, self-correcting once the
    check completes) rather than a silent CLEAN over a forged green.
    """
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "Backend Tests", "status": "IN_PROGRESS", "conclusion": ""},
            {"context": "PR Readiness", "state": "SUCCESS"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 10


def test_legacy_pull_request_without_aggregate_still_fails_closed() -> None:
    module = _load_script()
    payload = _pr_payload(
        [{"name": "Backend Tests", "status": "COMPLETED", "conclusion": "FAILURE"}]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_check_run_named_pr_readiness_cannot_mask_a_failure() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "Backend Tests", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_merged_pull_request_is_terminal_not_running() -> None:
    """A non-open PR must exit 20, not wait on mergeability GitHub never computes."""
    module = _load_script()
    payload = _pr_payload([], state="MERGED", mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_closed_pull_request_is_terminal_not_running() -> None:
    module = _load_script()
    payload = _pr_payload([], state="CLOSED", mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_open_pull_request_with_unknown_mergeability_still_waits() -> None:
    """The terminal-state check must not swallow the legitimate async wait."""
    module = _load_script()
    payload = _pr_payload(
        [{"name": "Backend Tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        mergeable="UNKNOWN",
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 10


def _check(
    name, conclusion, run, *, definition=7, event="pull_request", workflow="ci.yml", **extra
):
    """A COMPLETED CheckRun row of workflow run ``run``, in the flat shape the
    scripts read. The run's own conclusion defaults to the row's: a one-row run
    concludes as its job did. Pass ``run_conclusion`` to model a run whose
    verdict differs from this row's, e.g. a FAILURE run holding a CANCELLED row."""
    row = {
        "name": name,
        "workflowName": workflow,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "workflowRunId": run,
        "workflowRunEvent": event,
        "workflowDefinitionId": definition,
        "workflowRunConclusion": extra.pop("run_conclusion", conclusion),
    }
    if run is not None:
        # A job start that follows the run order, so a rule ordering rows by
        # startedAt reaches the SAME answer as one ordering by run id here; the
        # tests that separate the two pass their own startedAt.
        started = datetime(2026, 8, 6, tzinfo=timezone.utc) + timedelta(seconds=run)
        row["startedAt"] = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    row.update(extra)
    return row


def _exit_for(checks: list[dict]) -> int:
    module = _load_script()
    _install_fake_gh(module, _pr_payload(checks))
    return module.main(["pr_status.py", "42"])


def test_superseded_cancelled_run_does_not_count_as_a_failure() -> None:
    """A re-run leaves the CANCELLED attempt in the rollup; its run concluded
    CANCELLED (the concurrency group displaced it) and a newer run of the same
    check exists, so the row is dropped and the newest run's verdict stands."""
    assert (
        _exit_for([_check("GPT Review", "CANCELLED", 100), _check("GPT Review", "SUCCESS", 200)])
        == 0
    )


def test_superseded_success_does_not_mask_a_newer_failure() -> None:
    """A fresh failure stays red whatever the older run concluded."""
    assert (
        _exit_for(
            [_check("Backend Tests", "SUCCESS", 100), _check("Backend Tests", "FAILURE", 200)]
        )
        == 20
    )


def test_two_live_rows_of_one_run_both_survive_in_either_arrival_order() -> None:
    """A workflow can publish a check run under its own job's name,
    so ONE run emits two rows of one identity. They are not retries of each other.
    Both orders are exercised because one order passes for the wrong reason: a
    last-writer-wins collapse keeps whichever row arrived last."""
    live_failure = _check("Fork PR Description", "FAILURE", 300, startedAt="2026-09-18T11:22:36Z")
    live_success = _check("Fork PR Description", "SUCCESS", 300, startedAt="2026-09-18T11:22:38Z")
    assert _exit_for([live_failure, live_success]) == 20
    assert _exit_for([live_success, live_failure]) == 20


def test_two_rows_of_one_run_are_both_reported_not_only_counted() -> None:
    """Identity-level pin, independent of the exit code: neither row of a
    same-run pair is removed, in either arrival order."""
    module = _load_script()
    a = _check("Fork PR Description", "FAILURE", 300, startedAt="2026-09-18T11:22:36Z")
    b = _check("Fork PR Description", "SUCCESS", 300, startedAt="2026-09-18T11:22:38Z")
    assert module.collapse_superseded([a, b]) == [a, b]
    assert module.collapse_superseded([b, a]) == [b, a]


def test_same_check_name_in_different_workflows_stays_distinct() -> None:
    """Identity is the workflow DEFINITION: two files may declare one `name:`
    and each publish a check of the same name. The older run below was cancelled
    whole -- the one shape the collapse may drop -- but it belongs to an
    unrelated workflow, so nothing superseded it and its row stays."""
    older_cancelled = _check("build", "CANCELLED", 100, definition=1, workflow="Code Review")
    newer_success = _check("build", "SUCCESS", 200, definition=2, workflow="Code Review")
    assert _exit_for([older_cancelled, newer_success]) == 20


def test_two_triggers_of_one_workflow_are_concurrent_dispatches_not_attempts() -> None:
    """One file on `push` and `pull_request` dispatches two runs for one commit.
    Neither replaces the other: the cancelled push run below is the one shape
    the collapse may drop, yet a later pull_request run of the same definition
    did not supersede it, so its row stays and still counts."""
    push_cancelled = _check("Backend Tests", "CANCELLED", 100, event="push")
    pr_success = _check("Backend Tests", "SUCCESS", 200, event="pull_request")
    assert _exit_for([push_cancelled, pr_success]) == 20


def test_rows_without_a_run_identity_are_exempt_not_grouped_together() -> None:
    """A check run created outside Actions has no workflowRun. Two such rows must
    not be grouped under a missing key, which would drop all but the newest; each
    stays, so the older failure is still reported."""
    app_failure = {
        "name": "CodeQL",
        "status": "COMPLETED",
        "conclusion": "FAILURE",
        "startedAt": "2026-08-06T01:00:00Z",
    }
    app_success = {
        "name": "CodeQL",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "startedAt": "2026-08-06T02:00:00Z",
    }
    assert _exit_for([app_failure, app_success]) == 20
    assert _exit_for([app_success, app_failure]) == 20


def test_rows_missing_only_the_event_are_exempt_not_merged() -> None:
    """The event is non-null on the wire, so its absence is a truncated response.
    Keying on what is left would merge two triggers into one group and drop the
    cancelled run below as superseded; a withheld component exempts the row."""
    older = _check("Backend Tests", "CANCELLED", 100)
    newer = _check("Backend Tests", "SUCCESS", 200)
    older["workflowRunEvent"] = newer["workflowRunEvent"] = None
    assert _exit_for([older, newer]) == 20


def test_unordered_duplicates_are_all_kept_fail_closed() -> None:
    """Without a run id there is no ordering evidence, so neither may silently
    supersede the other -- even a cancelled row of a cancelled run, the one
    shape the collapse may drop, must survive."""
    unidentified = _check("Backend Tests", "CANCELLED", None, startedAt="2026-08-06T01:00:00Z")
    assert _exit_for([unidentified, _check("Backend Tests", "SUCCESS", 200)]) == 20


def test_a_replaced_rounds_failure_rows_stay_live() -> None:
    """The rollup carries no lineage edge: a later run of one identity may be an
    independent dispatch (`synchronize` and `edited` are both `pull_request`), so
    recency alone never licenses dropping a row. Only a run the concurrency group
    CANCELLED is proven displaced; a replaced round that concluded FAILURE keeps
    its rows live. Over-report rather than hide a live failure."""
    assert (
        _exit_for(
            [_check("Backend Tests", "FAILURE", 100), _check("Backend Tests", "SUCCESS", 200)]
        )
        == 20
    )


def test_a_cancelled_row_inside_a_live_run_is_not_dropped() -> None:
    """fail-fast, a failed `needs`, or an operator cancelling one job leave a
    CANCELLED row inside a run that concluded FAILURE. That run is live; the
    row's own cancellation is not displacement."""
    sibling_cancelled = _check("shard 2", "CANCELLED", 100, run_conclusion="FAILURE")
    newer_success = _check("shard 2", "SUCCESS", 200)
    assert _exit_for([sibling_cancelled, newer_success]) == 20


def test_a_cancelled_rows_verdict_must_also_be_cancelled_to_drop_it() -> None:
    """A cancelled run can still hold a row that reached a real FAILURE before
    the cancel landed. Removing it would take a verdict with it, so it stays."""
    decided_in_cancelled_run = _check("Backend Tests", "FAILURE", 100, run_conclusion="CANCELLED")
    assert _exit_for([decided_in_cancelled_run, _check("Backend Tests", "SUCCESS", 200)]) == 20


def test_a_cancelled_newest_run_is_never_dropped() -> None:
    """Dropping the newest run would revive the verdict it superseded: the
    older SUCCESS would read as the live result of a lane whose latest attempt
    did not finish."""
    assert (
        _exit_for(
            [_check("Backend Tests", "SUCCESS", 100), _check("Backend Tests", "CANCELLED", 200)]
        )
        == 20
    )


def test_newest_is_the_greatest_run_id_not_the_latest_started_row() -> None:
    """startedAt is when a JOB got a runner, and a busy queue starts an older
    run's job after a newer run's. Ordered by startedAt the displaced CANCELLED
    row below would be the 'newest' and survive as a failure; ordered by run id
    it is the older run and is dropped."""
    displaced_late_start = _check(
        "Backend Tests", "CANCELLED", 100, startedAt="2026-08-06T02:00:00Z"
    )
    newest_early_start = _check("Backend Tests", "SUCCESS", 200, startedAt="2026-08-06T01:00:00Z")
    assert _exit_for([displaced_late_start, newest_early_start]) == 0
    assert _exit_for([newest_early_start, displaced_late_start]) == 0


def test_a_displaced_round_is_dropped_whole_not_row_by_row() -> None:
    """Measured shape: the concurrency group cancelled run 100 whole,
    so every one of its rows is COMPLETED/CANCELLED under a CANCELLED run while
    run 200 carries the live verdicts. The board reads as run 200 alone."""
    displaced = [
        _check(name, "CANCELLED", 100, definition=314753039, workflow="Code Review")
        for name in ("Automated Rule Check", "Inclusive Language", "SAST (Semgrep)", "PR Hygiene")
    ]
    live = [
        _check(name, "SUCCESS", 200, definition=314753039, workflow="Code Review")
        for name in ("Automated Rule Check", "Inclusive Language", "SAST (Semgrep)", "PR Hygiene")
    ]
    module = _load_script()
    assert module.collapse_superseded(displaced + live) == live
    assert module.collapse_superseded(live + displaced) == live


def test_status_contexts_are_never_collapsed() -> None:
    """The host reports one status per context (the latest), so two rows of one
    context is not a shape a collapse has to order, and the provider keeps every
    status row. Keeping them all is the fail-closed direction: whatever the dates
    say, an older FAILURE beside a newer SUCCESS of one context still counts."""
    older_failure = {
        "context": "PR Readiness",
        "state": "FAILURE",
        "createdAt": "2026-08-06T01:00:00Z",
    }
    newer_success = {
        "context": "PR Readiness",
        "state": "SUCCESS",
        "createdAt": "2026-08-06T02:00:00Z",
    }
    assert _exit_for([older_failure, newer_success]) == 20
    assert _exit_for([newer_success, older_failure]) == 20
    module = _load_script()
    assert module.collapse_superseded([older_failure, newer_success]) == [
        older_failure,
        newer_success,
    ]


# --- the rollup read ----------------------------------------------------------
#
# `gh pr view --json statusCheckRollup` rows carry only `workflowName` for the
# workflow: no run id, no definition id, no event (measured on a live board). The
# collapse keys on those, so the rollup is read through GraphQL. The read keeps
# the contract the comment above ROLLUP_UNAVAILABLE_NOTICE states: its own call,
# soft degradation, the head re-checked beside the rollup.


def _rollup_read_calls(
    module: ModuleType, responses: list[tuple[int, str, str]]
) -> list[list[str]]:
    """Drive main() with the rollup read answered page by page from ``responses``;
    return the argv of every rollup read issued."""
    core = json.loads(_pr_payload([]))
    del core["statusCheckRollup"]
    pages = list(responses)
    seen: list[list[str]] = []

    def fake_run(args: list[str]) -> tuple[int, str, str]:
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps(core), ""
        if is_rollup_graphql_read(args):
            seen.append(args)
            return pages.pop(0) if pages else (1, "", "no more pages")
        if args[:3] == ["gh", "repo", "view"]:
            return 0, "example/repo", ""
        if args[:2] == ["gh", "api"] and "/issues/" in args[2] and "/comments" in args[2]:
            return 0, "[]", ""
        if args[:2] == ["gh", "api"] and "/actions/runs" in args[2]:
            return (
                0,
                json.dumps({"total_count": 1, "workflow_runs": [{"event": "pull_request"}]}),
                "",
            )
        if args[:1] == ["git"]:
            return _fake_git(args)
        raise AssertionError("unexpected command: {}".format(args))

    module.run = fake_run
    module.unresolved_thread_count = lambda _number: 0
    return seen


def test_the_rollup_query_selects_every_field_the_collapse_keys_on() -> None:
    """The fake answers whatever the fixture carries, so a field the query never
    asked for would pass every test above while the live read lacked it. Pin the
    selection bytes: the run id, event and definition id through the check
    suite, the suite's conclusion (the run's own verdict), and the row fields
    the rest of the report reads."""
    module = _load_script()
    seen = _rollup_read_calls(module, [(0, rollup_graphql_response([], "f" * 40), "")])
    assert module.main(["pr_status.py", "42"]) == 20  # no checks reported -> fail-closed
    assert len(seen) == 1
    query = next(a for a in seen[0] if a.startswith("query="))
    for selection in (
        "headRefOid",
        "commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100,after:$c)",
        "pageInfo{hasNextPage endCursor}",
        "... on CheckRun{name status conclusion startedAt detailsUrl "
        "checkSuite{conclusion workflowRun{databaseId event workflow{databaseId name}}}}",
        "... on StatusContext{context state targetUrl}",
    ):
        assert selection in query, selection
    # Owner, repository and number travel as variables, never in the text. The
    # strings go through `-f`: gh's typed `-F` turns an all-digit owner or
    # repository (`someorg/2048`) into a JSON integer the host rejects against
    # `String!`, and that repository would read as UNAVAILABLE forever.
    assert ["-f", "o=example", "-f", "r=repo", "-F", "n=42"] == seen[0][-6:]


def test_the_rollup_read_pages_and_re_checks_the_head_on_every_page() -> None:
    module = _load_script()
    head = "f" * 40
    first = rollup_graphql_response(
        [_check("a", "SUCCESS", 1)], head, has_next=True, end_cursor="c1"
    )
    second = rollup_graphql_response([_check("b", "FAILURE", 2)], head)
    seen = _rollup_read_calls(module, [(0, first, ""), (0, second, "")])

    assert module.main(["pr_status.py", "42"]) == 20  # the failure on page two counts
    assert len(seen) == 2
    assert not any(a.startswith("c=") for a in seen[0])
    # The cursor is an opaque string and travels as one: `-f`, not typed `-F`.
    assert seen[1][seen[1].index("c=c1") - 1] == "-f"


def test_a_head_that_moves_mid_pagination_discards_the_rollup(capsys) -> None:
    module = _load_script()
    first = rollup_graphql_response(
        [_check("a", "SUCCESS", 1)], "f" * 40, has_next=True, end_cursor="c1"
    )
    moved = rollup_graphql_response([_check("b", "SUCCESS", 2)], "b" * 40)
    _rollup_read_calls(module, [(0, first, ""), (0, moved, "")])

    assert module.main(["pr_status.py", "42"]) == 20
    out = capsys.readouterr().out
    assert "NOTICE: " + module.ROLLUP_HEAD_MOVED_NOTICE in out
    assert "- a:" not in out, "rows from the page before the move must not leak"


def test_a_rollup_whose_commit_is_not_the_reported_head_is_discarded(capsys) -> None:
    """Both comparisons are the same guard: the pull request's head and the
    commit the rollup hangs off must each be the head the core read reported."""
    module = _load_script()
    stale_commit = rollup_graphql_response(
        [{"context": "PR Readiness", "state": "SUCCESS"}], "f" * 40, commit_oid="a" * 40
    )
    _rollup_read_calls(module, [(0, stale_commit, "")])

    assert module.main(["pr_status.py", "42"]) == 20
    assert "NOTICE: " + module.ROLLUP_HEAD_MOVED_NOTICE in capsys.readouterr().out


def test_a_board_past_the_page_cap_reads_unknown_not_partial(capsys) -> None:
    """A partial read could keep a displaced row whose successor sits on the
    page never fetched, so a board that outruns the cap is UNKNOWN, fail-closed."""
    module = _load_script()
    endless = rollup_graphql_response(
        [_check("a", "SUCCESS", 1)], "f" * 40, has_next=True, end_cursor="again"
    )
    seen = _rollup_read_calls(module, [(0, endless, "")] * (module._MAX_ROLLUP_PAGES + 5))

    assert module.main(["pr_status.py", "42"]) == 20
    assert len(seen) == module._MAX_ROLLUP_PAGES
    out = capsys.readouterr().out
    assert "NOTICE: " + module.ROLLUP_UNAVAILABLE_NOTICE in out
    assert "- a:" not in out


def test_a_malformed_rollup_page_reads_unavailable(capsys) -> None:
    module = _load_script()
    for broken in (
        json.dumps({"data": {"repository": None}}),
        json.dumps(
            {"data": {"repository": {"pullRequest": {"headRefOid": "f" * 40, "commits": "nope"}}}}
        ),
    ):
        _rollup_read_calls(module, [(0, broken, "")])
        assert module.main(["pr_status.py", "42"]) == 20
        assert "NOTICE: " + module.ROLLUP_UNAVAILABLE_NOTICE in capsys.readouterr().out
