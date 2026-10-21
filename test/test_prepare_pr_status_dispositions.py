"""``pr_status.py``: the disposition gate, its ``--disposition-gate`` JSON contract and the
writer lookup it trusts.
"""

from __future__ import annotations

import json
from types import ModuleType

from prepare_pr_status_helpers import (
    _GATE_HEAD,
    _GREEN_CHECKS,
    _HEAD,
    _disposition,
    _gate_argv,
    _gate_bot_comment,
    _install_fake_gh,
    _last_line_json,
    _load_script,
    _pr_payload,
)

# ---------------------------------------------------------------------------
# The disposition gate -- one lane, one rationale per finding.
# The computation is pinned byte-identical to pr_findings.py's copy by
# test_prepare_pr_findings.py; these tests cover the GATING half.
# ---------------------------------------------------------------------------


def _gpt_finding_comment(module: ModuleType) -> tuple[dict, str]:
    """A trusted GPT-lane comment with one advisory finding for the head."""
    span = module.span_hash("src/x.py", "gpt/BLOCKING")
    comment = {
        # Real comments carry a node id; without one the supersession reader
        # cannot address the stored history and fails closed.
        "node_id": "IC_fixture",
        "user": {"type": "Bot", "login": "github-actions[bot]"},
        "body": (
            "<!-- codex-ai-review -->\n"
            "BLOCKING -- src/x.py:10 -- tighten -> Fix: x\n"
            "[GPT-REVIEWED] " + "f" * 40
        ),
    }
    return comment, span


def test_cross_lane_disposition_from_a_writer_blocks_readiness(capsys) -> None:
    """A writer-authored record whose target= lane differs from the lane of
    the span it claims is exactly the blanket ruling the rule forbids."""
    module = _load_script()
    bot_comment, span = _gpt_finding_comment(module)
    disposition = _disposition("alice", "opus", f"- **rebutted** span={span}\n> reason")
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([bot_comment, disposition]),
        permissions={"alice": "write"},
    )

    code = module.main(["pr_status.py", "42", "--json"])

    assert code == 20
    report = _last_line_json(capsys)
    assert "disposition rule:" in report["progress_key"]["status"]
    assert "cross-lane" in report["progress_key"]["status"]


def test_per_finding_same_lane_disposition_stays_clean(capsys) -> None:
    module = _load_script()
    bot_comment, span = _gpt_finding_comment(module)
    disposition = _disposition("alice", "gpt", f"- **rebutted** span={span}\n> reason")
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([bot_comment, disposition]),
        permissions={"alice": "write"},
    )

    assert module.main(["pr_status.py", "42"]) == 0


def test_spanless_disposition_for_a_lane_with_findings_blocks(capsys) -> None:
    """A blanket ruling naming no finding identity while its lane has findings
    on the current head."""
    module = _load_script()
    bot_comment, _span = _gpt_finding_comment(module)
    disposition = _disposition("alice", "gpt", "> out of scope for this fix")
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([bot_comment, disposition]),
        permissions={"alice": "write"},
    )

    code = module.main(["pr_status.py", "42", "--json"])

    assert code == 20
    assert "claims no span=" in _last_line_json(capsys)["progress_key"]["status"]


def test_non_writer_disposition_cannot_block_the_pr(capsys) -> None:
    """A drive-by commenter's crafted marker must not hold the PR hostage:
    authority comes from the collaborators permission, as in the ledger."""
    module = _load_script()
    bot_comment, span = _gpt_finding_comment(module)
    disposition = _disposition("mallory", "opus", f"- **rebutted** span={span}")
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([bot_comment, disposition]),
        permissions={"mallory": "read"},
    )

    assert module.main(["pr_status.py", "42"]) == 0


def test_unreadable_disposition_comments_fail_closed_in_decide() -> None:
    module = _load_script()

    code, status = module.decide(
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        decision="NONE",
        draft=False,
        readiness_kind="pass",
        n_running=0,
        n_fail=0,
        n_checks=1,
        readiness_context="PR Readiness",
        disposition_eval={"ok": False, "violations": []},
    )

    assert code == 20
    assert "disposition records could not be established" in status


def test_unreadable_disposition_evaluation_outranks_the_running_round() -> None:
    """ "Could not read" is not "no violations": while an unknown evaluation
    waits behind an in-flight round, the bots rewrite their stamped comments
    and the judged-head evidence a re-read would need is gone. It must gate
    NOW, exactly like a known violation."""
    module = _load_script()

    code, status = module.decide(
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        decision="NONE",
        draft=False,
        readiness_kind="running",
        n_running=3,
        n_fail=0,
        n_checks=5,
        readiness_context="PR Readiness",
        disposition_eval={"ok": False, "violations": []},
    )

    assert code == 20
    assert "disposition records could not be established" in status


def test_clean_disposition_eval_does_not_change_the_verdict() -> None:
    module = _load_script()

    code, _status = module.decide(
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        decision="NONE",
        draft=False,
        readiness_kind="pass",
        n_running=0,
        n_fail=0,
        n_checks=1,
        readiness_context="PR Readiness",
        disposition_eval={"ok": True, "violations": []},
    )

    assert code == 0


def test_disposition_violation_outranks_the_running_round(capsys) -> None:
    """Precedence: a violation is a condition waiting cannot fix -- only the
    author editing the comment clears it -- and deferring it behind an
    in-flight round loses the evidence, because the reviewer bots rewrite
    their stamped comments in place when the round completes. It must gate
    NOW, like a conflict or a draft, not after the checks settle."""
    module = _load_script()
    bot_comment, span = _gpt_finding_comment(module)
    disposition = _disposition("alice", "opus", f"span={span}")
    _install_fake_gh(
        module,
        _pr_payload([{"context": "PR Readiness", "state": "PENDING"}]),
        comments=json.dumps([bot_comment, disposition]),
        permissions={"alice": "write"},
    )

    code = module.main(["pr_status.py", "42", "--json"])

    assert code == 20
    assert "disposition rule:" in _last_line_json(capsys)["progress_key"]["status"]


def test_prior_head_record_still_blocks_after_the_fix_push(capsys) -> None:
    """The ordinary flow: the writer stamps head=<prior-reviewed-sha> and then
    pushes, so the PR head has moved by the time the gate polls. The record
    must be validated against the head it judged -- skipping it as history is
    exactly how a blanket ruling ships green."""
    module = _load_script()
    prior = "f" * 40
    current = "e" * 40
    span = module.span_hash("src/x.py", "gpt/BLOCKING")
    bot_comment = {
        # Real comments carry a node id; without one the supersession reader
        # cannot address the stored history and fails closed.
        "node_id": "IC_fixture",
        "user": {"type": "Bot", "login": "github-actions[bot]"},
        "body": (
            "<!-- codex-ai-review -->\n"
            "BLOCKING -- src/x.py:10 -- tighten -> Fix: x\n"
            "[GPT-REVIEWED] " + prior
        ),
    }
    disposition = {
        "id": 12,
        "user": {"type": "User", "login": "alice"},
        "body": (
            "<!-- ai-review-disposition target=opus head="
            + prior
            + " -->\n"
            + f"- **rebutted** span={span}\n> reason"
        ),
    }
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS, headRefOid=current),
        comments=json.dumps([bot_comment, disposition]),
        permissions={"alice": "write"},
    )

    code = module.main(["pr_status.py", "42", "--json"])

    assert code == 20
    status = _last_line_json(capsys)["progress_key"]["status"]
    assert "disposition rule:" in status
    assert "cross-lane" in status


# ---------------------------------------------------------------------------
# The disposition rule is enforced server-side, in pr-readiness.yml,
# by calling THIS script's --disposition-gate mode -- so the rule keeps one
# definition instead of gaining a workflow-side copy of the grammar. These pin
# the JSON contract that workflow step parses.
# ---------------------------------------------------------------------------


def test_disposition_gate_reports_a_blanket_record_as_a_violation(capsys) -> None:
    """The gap this closes: a writer skipping the kirocrew-prepare-pr loop posts a
    single-rationale record naming a lane but claiming no span, and the
    adjudication ledger admits it with full downgrade power. The gate must name
    it so the required status can fail."""
    module = _load_script()
    blanket = {
        "id": 900,
        "user": {"type": "User", "login": "alice"},
        "body": "<!-- ai-review-disposition target=gpt head=" + _GATE_HEAD + " -->\nall fine",
    }
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_gate_bot_comment(), blanket]),
        permissions={"alice": "write"},
    )

    assert module.main(_gate_argv()) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is True
    assert report["records"] == 1
    assert report["unverified"] == 0
    assert len(report["violations"]) == 1
    assert "claims no span= finding identity" in report["violations"][0]


def test_disposition_gate_is_clean_when_the_record_claims_one_span(capsys) -> None:
    module = _load_script()
    span = module.span_hash("src/x.py", "gpt/BLOCKING")
    ruling = {
        "id": 901,
        "user": {"type": "User", "login": "alice"},
        "body": (
            "<!-- ai-review-disposition target=gpt head="
            + _GATE_HEAD
            + " -->\n"
            + f"- **rebutted** span={span}\n> reason"
        ),
    }
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_gate_bot_comment(), ruling]),
        permissions={"alice": "write"},
    )

    assert module.main(_gate_argv()) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is True
    assert report["violations"] == []
    assert report["error"] == ""


def test_disposition_gate_drops_a_record_whose_author_is_not_a_writer(capsys) -> None:
    """Enforcement scope equals the ledger's admission scope. An author the
    permission API does not confirm is dropped exactly as codex-review.yml
    drops them, so the gate never blocks on a record with no downgrade power --
    and that includes the case where the permission call itself fails."""
    module = _load_script()
    blanket = {
        "id": 902,
        "user": {"type": "User", "login": "mallory"},
        "body": "<!-- ai-review-disposition target=gpt head=" + _GATE_HEAD + " -->\nlooks fine",
    }
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_gate_bot_comment(), blanket]),
        permissions={},
    )

    assert module.main(_gate_argv()) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is True
    assert report["violations"] == []
    assert report["comments"] == 1
    assert report["records"] == 0
    assert report["unverified"] == 1


def test_disposition_gate_reports_unreadable_comments_as_not_ok(capsys) -> None:
    """An unreadable comment list is UNKNOWN, never a clean rule: the workflow
    turns ok=false into a pending verdict, so a transient API failure can never
    red the repository's required status."""
    module = _load_script()

    def failing_run(args: list[str]) -> tuple[int, str, str]:
        if args[:2] == ["gh", "api"]:
            return 1, "", "gh: Server Error (HTTP 500)"
        raise AssertionError("unexpected command: {}".format(args))

    module.run = failing_run

    assert module.main(_gate_argv()) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is False
    assert "could not be read" in report["error"]
    assert report["violations"] == []


def test_disposition_gate_reads_the_comment_pages_once(capsys) -> None:
    """The records and the marker comments come from ONE paginated read.

    This mode runs on every full readiness evaluation, and each page is a
    request on the hourly GITHUB_TOKEN pool every workflow shares. A second
    walk of the same pages bought nothing: both selectors filter the same
    list. Two full pages plus a short one pin that pagination still reaches
    the last page, and that each page is requested exactly once.
    """
    module = _load_script()
    span = module.span_hash("src/x.py", "gpt/BLOCKING")
    ruling = {
        "id": 903,
        "user": {"type": "User", "login": "alice"},
        "body": (
            "<!-- ai-review-disposition target=gpt head="
            + _GATE_HEAD
            + " -->\n"
            + f"- **rebutted** span={span}\n> reason"
        ),
    }
    filler = [{"id": i, "user": {"type": "User", "login": "bob"}, "body": "hi"} for i in range(100)]
    pages = {1: filler, 2: filler, 3: [_gate_bot_comment(), ruling]}
    requested: list[str] = []

    def fake_run(args: list[str]) -> tuple[int, str, str]:
        if args[:2] == ["gh", "api"] and "/collaborators/" in args[2]:
            return 0, json.dumps({"permission": "write"}), ""
        if args[:2] == ["gh", "api"] and "/issues/42/comments" in args[2]:
            requested.append(args[2])
            page = int(args[2].rsplit("page=", 1)[1])
            return 0, json.dumps(pages[page]), ""
        raise AssertionError("unexpected command: {}".format(args))

    module.run = fake_run

    assert module.main(_gate_argv()) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is True, report
    assert report["records"] == 1
    assert report["violations"] == []
    assert [url.rsplit("page=", 1)[1] for url in requested] == ["1", "2", "3"]


def test_the_shared_comment_read_keeps_only_what_a_selector_wants() -> None:
    """Filtering page by page: a comment neither selector wants is dropped as
    its page is read, so a PR with thousands of ordinary comments does not
    hold them all in memory."""
    module = _load_script()
    ordinary = {"id": 1, "user": {"type": "User", "login": "bob"}, "body": "x" * 1000}
    marker = _gate_bot_comment()

    def fake_run(args: list[str]) -> tuple[int, str, str]:
        return 0, json.dumps([ordinary, marker]), ""

    module.run = fake_run
    kept = module.fetch_issue_comments(
        "example/repo",
        42,
        keep=lambda c: module.is_trusted_bot_comment(c, ("github-actions[bot]",)),
    )
    assert kept == [marker]


def test_disposition_gate_requires_repo_pr_and_head(capsys) -> None:
    module = _load_script()

    assert module.main(["pr_status.py", "--disposition-gate", "--repo", "example/repo"]) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is False
    assert "required" in report["error"]


def test_disposition_gate_flattens_newlines_out_of_each_violation(capsys) -> None:
    """The workflow reads one violation per line, so a newline inside one would
    forge an extra blocker line. Flattening is what makes that unrepresentable."""
    module = _load_script()
    module.fetch_issue_comments = lambda *_a, **_k: []
    module.writer_disposition_records = lambda *_a: []
    module.disposition_violations = lambda *_a: ["first\nsecond   third"]

    assert module.main(_gate_argv()) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["violations"] == ["first second third"]


# ---------------------------------------------------------------------------
# An INDETERMINATE writer lookup must not read as "not a
# writer". The adjudication ledger makes the identical lookup at review time, so
# it can have admitted a record whose later verification here fails transiently
# -- dropping it would leave the record's downgrade power intact while the
# required status published success.
# ---------------------------------------------------------------------------


def _verdict_module(rc: int, out: str, err: str) -> ModuleType:
    module = _load_script()
    module.run = lambda _args: (rc, out, err)
    return module


def test_a_transient_permission_failure_is_unknown_not_a_denial() -> None:
    module = _verdict_module(1, "", "gh: Server Error (HTTP 500)")
    assert module.author_write_verdict("o/r", "alice") == "unknown"
    # The boolean face still reads False, so a record is never ACTED on without
    # positive confirmation -- the two answers differ only for a caller that
    # must distinguish "no" from "cannot tell".
    assert module.author_is_repo_writer("o/r", "alice") is False


def test_a_404_is_a_definitive_non_collaborator() -> None:
    module = _verdict_module(1, "", "gh: Not Found (HTTP 404)")
    assert module.author_write_verdict("o/r", "mallory") == "other"


def test_a_403_is_definitive_so_a_scoped_token_cannot_block_every_pr() -> None:
    """Calling a token-permission state "unknown" would turn it into a permanent
    cannot-evaluate on every PR carrying any disposition comment -- trading a
    missing enforcement for a repository-wide merge block."""
    module = _verdict_module(1, "", "gh: Resource not accessible by integration (HTTP 403)")
    assert module.author_write_verdict("o/r", "alice") == "other"


def test_a_rate_limit_403_is_transient_not_definitive() -> None:
    """The one 403 that is NOT a stable token property. GitHub's primary and
    secondary rate limits surface as 403 with rate-limit text, which is transient
    exactly like a 429 -- the same carve-out pr-readiness.yml's gh_retry helper
    already makes. Treating it as definitive would drop a valid writer's record
    under load and publish a clean verdict over a real violation."""
    module = _verdict_module(1, "", "gh: API rate limit exceeded for installation (HTTP 403)")
    assert module.author_write_verdict("o/r", "alice") == "unknown"


def test_a_secondary_rate_limit_403_is_also_transient() -> None:
    module = _verdict_module(1, "", "gh: You have exceeded a secondary rate limit (HTTP 403)")
    assert module.author_write_verdict("o/r", "alice") == "unknown"


def test_an_abuse_detection_403_is_also_transient() -> None:
    module = _verdict_module(
        1, "", "gh: You have triggered an abuse detection mechanism (HTTP 403)"
    )
    assert module.author_write_verdict("o/r", "alice") == "unknown"


def test_a_write_permission_is_a_writer() -> None:
    module = _verdict_module(0, json.dumps({"permission": "write"}), "")
    assert module.author_write_verdict("o/r", "alice") == "writer"


def test_a_read_permission_is_definitively_other() -> None:
    module = _verdict_module(0, json.dumps({"permission": "read"}), "")
    assert module.author_write_verdict("o/r", "alice") == "other"


def test_an_unparseable_body_is_unknown() -> None:
    module = _verdict_module(0, "not json", "")
    assert module.author_write_verdict("o/r", "alice") == "unknown"


def test_an_indeterminate_author_makes_the_record_set_unestablished() -> None:
    module = _load_script()
    module.author_write_verdict = lambda _repo, _login: "unknown"
    comments = [
        {
            "id": 1,
            "user": {"type": "User", "login": "alice"},
            "body": "<!-- ai-review-disposition target=gpt head=" + _HEAD + " -->\nruling",
        }
    ]
    assert module.writer_disposition_records("o/r", comments) is None


def test_a_definitive_non_writer_is_still_just_dropped() -> None:
    module = _load_script()
    module.author_write_verdict = lambda _repo, _login: "other"
    comments = [
        {
            "id": 1,
            "user": {"type": "User", "login": "mallory"},
            "body": "<!-- ai-review-disposition target=gpt head=" + _HEAD + " -->\nruling",
        }
    ]
    assert module.writer_disposition_records("o/r", comments) == []


def test_the_gate_reports_an_indeterminate_permission_as_not_ok(capsys) -> None:
    """End to end: the workflow reads ok=false as WAITING, so a transient
    permission failure holds the required status pending instead of publishing a
    clean rule it could not verify."""
    module = _load_script()
    blanket = {
        "id": 903,
        "user": {"type": "User", "login": "alice"},
        "body": "<!-- ai-review-disposition target=gpt head=" + _GATE_HEAD + " -->\nno span",
    }

    def flaky_run(args: list[str]) -> tuple[int, str, str]:
        if "/collaborators/" in args[2]:
            return 1, "", "gh: Server Error (HTTP 500)"
        if "/issues/" in args[2] and "/comments" in args[2]:
            return 0, json.dumps([_gate_bot_comment(), blanket]), ""
        raise AssertionError("unexpected command: {}".format(args))

    module.run = flaky_run

    assert module.main(_gate_argv()) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is False
    assert report["violations"] == []
