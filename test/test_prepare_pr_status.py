"""Regression tests for the prepare-pr aggregate readiness policy."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "scripts" / "pr_status.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_pr_status", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pr_payload(checks: list[dict[str, str]], **overrides: object) -> str:
    payload: dict[str, object] = {
        "number": 42,
        "title": "fix: keep the change focused",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "REVIEW_REQUIRED",
        "url": "https://github.com/example/repo/pull/42",
        "headRefName": "fix/focused",
        "statusCheckRollup": checks,
        # A resolved issue link, so unrelated tests do not emit the advisory
        # NOTICE line. It is NOT a CLEAN precondition -- the issue-link check
        # never changes the exit code. Tests that exercise it override these.
        "body": "Fixes #7",
        "closingIssuesReferences": [{"number": 7}],
        "headRefOid": "f" * 40,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _install_fake_gh(
    module: ModuleType,
    payload: str,
    comments: str = "[]",
    head_run_events: list[str] | None = None,
) -> None:
    events = ["pull_request"] if head_run_events is None else head_run_events

    def fake_run(args: list[str]) -> tuple[int, str, str]:
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            return 0, payload, ""
        if args[:3] == ["gh", "repo", "view"]:
            return 0, "example/repo", ""
        if args[:2] == ["gh", "api"] and "/issues/" in args[2] and "/comments" in args[2]:
            return 0, comments, ""
        if args[:2] == ["gh", "api"] and "/actions/runs" in args[2]:
            runs = [{"event": e} for e in events]
            return 0, json.dumps({"total_count": len(runs), "workflow_runs": runs}), ""
        raise AssertionError("unexpected command: {}".format(args))

    module.run = fake_run
    module.unresolved_thread_count = lambda _number: 3


def test_passed_aggregate_overrides_old_failures_and_advisory_threads() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "old duplicate check", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"context": "PR Readiness", "state": "SUCCESS"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


def test_passed_aggregate_overrides_an_old_pending_check() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "old duplicate check", "status": "IN_PROGRESS", "conclusion": ""},
            {"context": "PR Readiness", "state": "SUCCESS"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


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


def test_superseded_cancelled_run_does_not_count_as_a_failure() -> None:
    """A re-run leaves the CANCELLED attempt in the rollup; newest run wins."""
    module = _load_script()
    payload = _pr_payload(
        [
            {
                "name": "GPT Review",
                "workflowName": "review.yml",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "startedAt": "2026-08-06T01:00:00Z",
            },
            {
                "name": "GPT Review",
                "workflowName": "review.yml",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-06T02:00:00Z",
            },
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


def test_superseded_success_does_not_mask_a_newer_failure() -> None:
    """Newest-wins must work in both directions: a fresh failure stays red."""
    module = _load_script()
    payload = _pr_payload(
        [
            {
                "name": "Backend Tests",
                "workflowName": "ci.yml",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-06T01:00:00Z",
            },
            {
                "name": "Backend Tests",
                "workflowName": "ci.yml",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-06T02:00:00Z",
            },
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_same_check_name_in_different_workflows_stays_distinct() -> None:
    """Identity is workflow-qualified: two workflows may share a job name."""
    module = _load_script()
    payload = _pr_payload(
        [
            {
                "name": "build",
                "workflowName": "linux.yml",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-06T02:00:00Z",
            },
            {
                "name": "build",
                "workflowName": "windows.yml",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-08-06T01:00:00Z",
            },
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_unordered_duplicates_are_all_kept_fail_closed() -> None:
    """Without startedAt on both entries there is no ordering evidence, so
    neither may silently supersede the other -- the failure must survive."""
    module = _load_script()
    payload = _pr_payload(
        [
            {
                "name": "Backend Tests",
                "workflowName": "ci.yml",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            },
            {
                "name": "Backend Tests",
                "workflowName": "ci.yml",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-06T02:00:00Z",
            },
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_status_contexts_collapse_by_context_name() -> None:
    """StatusContexts share the identity axis via their context string."""
    module = _load_script()
    payload = _pr_payload(
        [
            {"context": "PR Readiness", "state": "FAILURE", "startedAt": "2026-08-06T01:00:00Z"},
            {"context": "PR Readiness", "state": "SUCCESS", "startedAt": "2026-08-06T02:00:00Z"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


# --- issue-link advisory (closing keyword) ------------------------------------
#
# The advisory exists because finished work merged with only "Related: #n" left
# the issue open forever, with nothing downstream to reconcile it. The host's own
# closingIssuesReferences resolution is the truth; the body regexes only
# classify WHY it resolved to nothing, so the operator is told which of the
# three mistakes they made.


def test_resolved_closing_reference_silences_the_notice() -> None:
    module = _load_script()
    assert module.closing_link_reason("Fixes #7", [{"number": 7}]) is None


def test_bare_reference_without_a_verb_is_reported() -> None:
    """The exact shape that merged in #2433/#2439 and closed nothing.

    Reported, not blocked -- the author decides.
    """
    module = _load_script()
    reason = module.closing_link_reason("Related: #2368, #2375 for context", [])
    assert reason is not None
    assert "no closing keyword" in reason


def test_verb_present_but_host_resolved_nothing_is_reported_distinctly() -> None:
    module = _load_script()
    reason = module.closing_link_reason("Fixes #999999", [])
    assert reason is not None
    assert "resolved no issue" in reason
    # Must NOT be reported as the missing-verb case; the operator needs to know
    # the verb is fine and the NUMBER is the problem.
    assert "no closing keyword" not in reason


def test_no_reference_at_all_is_reported_with_the_opt_out_named() -> None:
    module = _load_script()
    reason = module.closing_link_reason("A pure refactor with no tracked issue.", [])
    assert reason is not None
    assert "no issue closed" in reason


def test_explicit_opt_out_silences_the_notice() -> None:
    module = _load_script()
    body = "A pure refactor.\n\nno issue closed: no ticket exists for this cleanup."
    assert module.closing_link_reason(body, []) is None


def test_opt_out_must_be_a_trailer_not_a_mention() -> None:
    """Prose that merely discusses the check must NOT read as a declaration.

    An unanchored substring match let any body containing the phrase pass —
    including a body that only explains what the phrase is for.
    """
    module = _load_script()
    prose = "The gate accepts a `no issue closed: <why>` line as an opt-out."
    assert module.closing_link_reason(prose, []) is not None
    indented = "  no issue closed: buried in an instruction block"
    assert module.closing_link_reason(indented, []) is not None
    assert module.closing_link_reason("no issue closed but I forgot the colon", []) is not None


def test_shipped_body_template_does_not_read_as_a_declaration() -> None:
    """An author who copies the template and skips the Issue link section must
    still see the notice -- the leftover instruction text must not read as a
    declaration.

    This runs the real regexes against the real shipped asset, so the template
    and the check cannot drift back into agreeing. The template deliberately
    contains no column-0 opt-out declaration and no resolvable `#<digits>`.
    """
    module = _load_script()
    template = (
        SCRIPT.parent.parent / "assets" / "pr-body-template.md"
    ).read_text(encoding="utf-8")
    reason = module.closing_link_reason(template, [])
    assert reason is not None, "unfilled template reads as an issue-link declaration"
    assert "no issue link" in reason


def test_markdown_headings_are_not_mistaken_for_issue_references() -> None:
    """`# Problem` must not read as a bare `#n` ref, or every PR reports the
    wrong reason."""
    module = _load_script()
    reason = module.closing_link_reason("# Problem\n\n## Why it matters\n", [])
    assert reason is not None
    assert "no issue link" in reason


def test_missing_body_is_treated_as_no_link_not_a_crash() -> None:
    module = _load_script()
    assert module.closing_link_reason(None, []) is not None


def test_gh_query_requests_the_issue_link_fields() -> None:
    """The fake gh injects a payload directly, so no other test would notice the
    real ``--json`` field list dropping these two names -- the advisory would
    then always see an absent body and mis-report on every live PR."""
    module = _load_script()
    seen: list[str] = []

    def capture(args: list[str]) -> tuple[int, str, str]:
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            seen.append(args[args.index("--json") + 1])
            return 1, "", "stop here"
        raise AssertionError("unexpected command: {}".format(args))

    module.run = capture
    module.main(["pr_status.py", "42"])
    assert seen, "gh pr view was never called"
    assert "body" in seen[0].split(","), seen[0]
    assert "closingIssuesReferences" in seen[0].split(","), seen[0]


def test_missing_issue_link_is_reported_but_does_not_block(capsys) -> None:
    """The advisory must be VISIBLE and must NOT change the verdict.

    Both halves matter. Printing without asserting CLEAN would let the check
    silently regain gate power; asserting CLEAN without reading the output
    would pass even if the notice were deleted.
    """
    module = _load_script()
    checks = [
        {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    _install_fake_gh(module, _pr_payload(checks, body="Related: #7", closingIssuesReferences=[]))
    assert module.main(["pr_status.py", "42"]) == 0
    out = capsys.readouterr().out
    assert "STATUS: CLEAN" in out, out
    assert "closes on merge: nothing" in out, out
    assert "NOTICE:" in out and "no closing keyword" in out, out


def test_resolved_issue_link_reports_the_number_and_no_notice(capsys) -> None:
    module = _load_script()
    checks = [
        {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    _install_fake_gh(
        module,
        _pr_payload(checks, body="Fixes #7", closingIssuesReferences=[{"number": 7}]),
    )
    assert module.main(["pr_status.py", "42"]) == 0
    out = capsys.readouterr().out
    assert "closes on merge: #7" in out, out
    assert "NOTICE:" not in out, out


# ---------------------------------------------------------------------------
# Issue #2550: reviewer-marker freshness + blocking markers + head-run
# assertion move from babysit prose into the script.
# ---------------------------------------------------------------------------

_HEAD = "f" * 40
_OLD = "a" * 40


def _bot_comment(
    body: str,
    user_type: str = "Bot",
    login: str = "github-actions[bot]",
    key: str | None = "codex-ai-review",
) -> dict[str, object]:
    prefix = f"<!-- {key} -->\n" if key else ""
    return {"user": {"type": user_type, "login": login}, "body": prefix + body}


def _clean_checks() -> list[dict[str, str]]:
    return [{"context": "PR Readiness", "state": "SUCCESS"}]


def test_fresh_stamps_with_no_block_marker_stay_clean() -> None:
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
            _bot_comment(f"No findings.\n[OPUS-REVIEWED] {_HEAD}"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 0


def test_stale_reviewer_stamp_blocks_a_would_be_clean_pr() -> None:
    """A stamp naming an older head means this head was never reviewed."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_OLD}"),
            _bot_comment(f"No findings.\n[OPUS-REVIEWED] {_HEAD}"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 20


def test_block_merge_for_current_head_blocks_even_when_readiness_passed() -> None:
    """The check conclusion is untrusted; the body marker is the signal."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(
                "BLOCKING -- src/x.py:10 -- broken\n"
                f"[GPT-REVIEWED] {_HEAD}\n[BLOCK-MERGE] {_HEAD}"
            ),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 20


def test_block_merge_for_an_older_head_does_not_block() -> None:
    """Bots update in place; a marker for a superseded head is history."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(f"[GPT-REVIEWED] {_OLD}\n[BLOCK-MERGE] {_OLD}"),
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 0


def test_non_blocking_findings_never_change_the_exit_code() -> None:
    """Advisory findings are a judgment call, deliberately left to prose."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(
                "FINDING -- src/x.py:10 -- could be tighter -> Fix: tighten\n"
                f"[GPT-REVIEWED] {_HEAD}"
            ),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 0


def test_unreadable_comments_fail_closed() -> None:
    module = _load_script()

    def fake_run(args: list[str]) -> tuple[int, str, str]:
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            return 0, _pr_payload(_clean_checks()), ""
        if args[:3] == ["gh", "repo", "view"]:
            return 0, "example/repo", ""
        if args[:2] == ["gh", "api"]:
            return 1, "", "boom"
        raise AssertionError("unexpected command: {}".format(args))

    module.run = fake_run
    module.unresolved_thread_count = lambda _n: 0

    assert module.main(["pr_status.py", "42"]) == 20


def test_stamps_from_non_bot_users_are_ignored() -> None:
    """A human quoting the marker text must not create a reviewer identity."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(f"see [FOO-REVIEWED] {_OLD} above", user_type="User"),
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 0


def test_unbound_stamps_do_not_gate_and_the_filter_still_pins() -> None:
    """Identity comes from the workflow-authored comment key: a stamp for a
    name with no bound lane is model output, not a reviewer, so it neither
    grants nor blocks. Pinning via --reviewers still requires bound lanes."""
    module = _load_script()
    comments = json.dumps(
        [
            # Un-keyed comment carrying a stale stamp: contributes nothing.
            _bot_comment(f"[SOMEBOT-REVIEWED] {_OLD}", key=None),
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    # Discovery mode: only bound lanes that posted are held; GPT is fresh.
    assert module.main(["pr_status.py", "42"]) == 0
    # Pinning GPT alone stays clean; pinning OPUS too blocks (no OPUS lane).
    assert module.main(["pr_status.py", "42", "--reviewers", "GPT"]) == 0
    assert module.main(["pr_status.py", "42", "--reviewers", "GPT,OPUS"]) == 20


def test_block_merge_gates_even_when_its_reviewer_is_filtered_out() -> None:
    """An explicit block marker for this head fails closed past any filter."""
    module = _load_script()
    comments = json.dumps(
        [_bot_comment(f"[SOMEBOT-REVIEWED] {_HEAD}\n[BLOCK-MERGE] {_HEAD}")]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42", "--reviewers", "GPT"]) == 20


def test_stale_stamp_is_not_evaluated_while_the_round_is_running() -> None:
    """Mid-round the bots have not posted for the new head yet: wait, not act."""
    module = _load_script()
    comments = json.dumps([_bot_comment(f"[GPT-REVIEWED] {_OLD}")])
    payload = _pr_payload([{"context": "PR Readiness", "state": "PENDING"}])
    _install_fake_gh(module, payload, comments=comments)

    assert module.main(["pr_status.py", "42"]) == 10


def test_missing_pull_request_run_for_head_blocks_actions_shaped_pr() -> None:
    """Zero runs of any event for the head means the visible checks are stale."""
    module = _load_script()
    checks = [
        {
            "name": "tests",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "workflowName": "CI",
        }
    ]
    _install_fake_gh(module, _pr_payload(checks), head_run_events=[])

    assert module.main(["pr_status.py", "42"]) == 20


def test_head_driven_by_other_events_is_not_held_to_pull_request() -> None:
    """A head whose CI runs on push/pull_request_target/workflow_run is never
    held to an event its repo does not use for it -- repo-wide history must
    not decide this (a repo that switched triggers retains old runs)."""
    module = _load_script()
    checks = [
        {
            "name": "tests",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "workflowName": "CI",
        }
    ]
    for events in (["push"], ["pull_request_target"], ["workflow_run", "push"]):
        _install_fake_gh(module, _pr_payload(checks), head_run_events=events)
        assert module.main(["pr_status.py", "42"]) == 0


def test_head_run_check_can_be_disabled_via_flag() -> None:
    """--head-run-check=off is the field escape hatch for repo shapes the
    event heuristic misreads; the gate degrades to pre-existing behavior."""
    module = _load_script()
    checks = [
        {
            "name": "tests",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "workflowName": "CI",
        }
    ]
    _install_fake_gh(module, _pr_payload(checks), head_run_events=[])

    assert module.main(["pr_status.py", "42"]) == 20
    assert module.main(["pr_status.py", "42", "--head-run-check", "off"]) == 0


def test_present_pull_request_run_for_head_stays_clean() -> None:
    module = _load_script()
    checks = [
        {
            "name": "tests",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "workflowName": "CI",
        }
    ]
    _install_fake_gh(module, _pr_payload(checks), head_run_events=["pull_request"])

    assert module.main(["pr_status.py", "42"]) == 0


def test_run_assertion_skipped_when_rollup_is_not_actions_shaped() -> None:
    """A repo reporting only legacy statuses must not be held to Actions."""
    module = _load_script()
    # No workflowName anywhere -> the runs endpoint must not even be queried.
    _install_fake_gh(module, _pr_payload(_clean_checks()), head_run_events=[])

    assert module.main(["pr_status.py", "42"]) == 0


def test_repo_is_derived_from_the_viewed_pr_url_not_the_cwd() -> None:
    """A full PR URL for a foreign repo must be evaluated against THAT repo --
    querying the checkout's repo would silently read the wrong comments/runs
    and the marker gates would be vacuous."""
    module = _load_script()
    assert (
        module.detect_repo("https://github.com/other-org/other-repo/pull/9")
        == "other-org/other-repo"
    )
    # No URL -> falls back to the cwd's repo via gh (exercised by every other
    # test through _install_fake_gh's `gh repo view` stub).


def test_named_reviewer_that_never_stamped_reads_as_stale() -> None:
    """--reviewers pins the fleet: a pinned reviewer with no fresh stamp must
    block, or an emitter drift / a bot that fails to post makes the gate
    silently vacuous (no stamps discovered -> exit 0 on an unreviewed head)."""
    module = _load_script()
    comments = json.dumps([_bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}")])
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    # GPT alone: present and fresh -> clean.
    assert module.main(["pr_status.py", "42", "--reviewers", "GPT"]) == 0
    # OPUS pinned but absent -> required, reads as stale -> blocked.
    assert module.main(["pr_status.py", "42", "--reviewers", "GPT,OPUS"]) == 20


def test_markers_from_untrusted_bot_logins_are_ignored() -> None:
    """`user.type == "Bot"` alone is spoofable: a third-party app echoing
    PR-controlled text could post a forged [<NAME>-REVIEWED]/[BLOCK-MERGE]
    marker. Only the emitting workflows' actor is trusted by default."""
    module = _load_script()
    comments = json.dumps(
        [
            # Forged block marker from a third-party app: must not gate.
            _bot_comment(f"[EVIL-REVIEWED] {_HEAD}\n[BLOCK-MERGE] {_HEAD}", login="coverage-app[bot]"),
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 0
    # And a forged FRESH stamp cannot satisfy a pinned reviewer either.
    comments_forged = json.dumps(
        [_bot_comment(f"[OPUS-REVIEWED] {_HEAD}", login="coverage-app[bot]")]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments_forged)
    assert module.main(["pr_status.py", "42", "--reviewers", "OPUS"]) == 20


def test_injected_stamp_for_another_reviewer_cannot_forge_freshness() -> None:
    """Reviewer model output is prompt-injectable via the diff: a stamp for
    ANOTHER reviewer's name inside a lane's comment is injected text and must
    not grant that reviewer's freshness. The lane's OWN stamp stays valid --
    identity comes from the workflow-authored comment key, not stamp names --
    and a [BLOCK-MERGE] still gates (injection can deny, never forge)."""
    module = _load_script()
    # GPT's lane carries an injected OPUS stamp; no real Opus comment.
    comments = json.dumps(
        [
            _bot_comment(
                f"No findings.\n[GPT-REVIEWED] {_HEAD}\n[OPUS-REVIEWED] {_HEAD}"
            ),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    # The forged OPUS stamp grants nothing: pinned OPUS reads as stale.
    assert module.main(["pr_status.py", "42", "--reviewers", "GPT,OPUS"]) == 20
    # GPT's own stamp in its own lane remains valid.
    assert module.main(["pr_status.py", "42", "--reviewers", "GPT"]) == 0
    # A [BLOCK-MERGE] in the lane still gates.
    comments_block = json.dumps(
        [
            _bot_comment(
                f"[GPT-REVIEWED] {_HEAD}\n[OPUS-REVIEWED] {_HEAD}\n[BLOCK-MERGE] {_HEAD}"
            ),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments_block)
    assert module.main(["pr_status.py", "42"]) == 20


def test_lane_emitting_only_another_reviewers_stamp_grants_nothing() -> None:
    """The exact forgery scenario: a malicious diff makes the UX lane emit a
    valid-looking verdict containing only [DESIGN-REVIEWED] while the real
    Design lane errors. The UX comment's key binds it to UX, so the DESIGN
    stamp inside it is ignored and Design stays stale."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(f"looks fine\n[DESIGN-REVIEWED] {_HEAD}", key="ux-review"),
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42", "--reviewers", "GPT,DESIGN"]) == 20


def test_stampless_advisory_lane_comment_does_not_block_discovery_mode() -> None:
    """The UX/Design workflows rewrite their keyed comment to a stampless
    'skipped' / 'could not complete' notice by design (advisory lanes must
    not block). A bound lane with zero stamps is 'not reviewed / not
    required' in discovery mode -- but a PINNED lane stays required."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment("⏭️ skipped: no UI changes in this revision", key="ux-review"),
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    # Discovery: the stampless UX lane is not required -> clean.
    assert module.main(["pr_status.py", "42"]) == 0
    # Pinned: UX is explicitly required -> its stampless state blocks.
    assert module.main(["pr_status.py", "42", "--reviewers", "GPT,UX"]) == 20
