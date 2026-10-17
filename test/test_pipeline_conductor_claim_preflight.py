"""Claim preflight — the conductor's deterministic claim predicate.

Every verdict branch gets a test, and each of those tests also asserts that the
single-question predicate this script replaces would have said CLAIM on the same
item. That second assertion is the point: the three real failures behind the
script (a merged PR an ``--state open`` query cannot see, an open PR the one
field read came back empty for, a claim written in prose) are all shaped the
same way, so a test that only checks the new answer would not notice the script
regressing back into the old blind spot.

The closure-pattern guards and the other checks' detectors live in
``test_pipeline_conductor_claim_preflight_check_detectors.py``, ``main()`` end to end
in ``test_pipeline_conductor_claim_preflight_end_to_end.py``, and the fixtures they
share in ``pipeline_conductor_claim_preflight_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pipeline_conductor_claim_preflight_helpers import mod_fixture  # noqa: F401
from pipeline_conductor_claim_preflight_helpers import (
    ITEM,
    REPO,
    SCRIPT,
    Forge,
    a_comment,
    a_pull,
    an_issue,
    clean_checks,
    naive_claim,
    run_main,
)


def a_forge_claim(label: str | None = "claimed", *assignees: str, foreign: bool = True) -> dict:
    """A ``forge_claim`` check value, as the detector would have produced it."""
    return {"label": label, "assignees": list(assignees), "foreign": foreign}


class TestVerdictPrecedence:
    """One test per precedence branch, on fabricated checks — no forge, no git."""

    def test_merged_pr_that_landed_is_already_fixed(self, mod):
        checks = clean_checks(
            merged_prs=[
                {
                    "number": 7900,
                    "author": "somebody",
                    "is_cross_repository": False,
                    "merge_commit_sha": "abc1234def567890",
                    "landed": True,
                    "closes_item": True,
                }
            ]
        )
        assert naive_claim(checks) is True  # the old predicate would dispatch
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("CLOSE", "already-fixed")
        assert evidence == {"pr": 7900, "sha": "abc1234def", "landed": True}
        assert mod.EXIT_CODES[name] == 11
        assert (
            mod.human_line(ITEM, name, reason, evidence, "low")
            == f"CLOSE {ITEM} merged-pr=#7900 sha=abc1234def landed=true"
        )

    def test_a_merged_pr_that_only_mentions_the_item_is_not_coverage(self, mod):
        """A mention is not closure. Reading it as coverage would CLOSE live
        work; reading it as a claim would starve an item whose fix was partial.
        So it decides nothing and falls through."""
        checks = clean_checks(
            merged_prs=[
                {
                    "number": 7902,
                    "merge_commit_sha": "abc1234def567890",
                    "landed": True,
                    "closes_item": False,
                }
            ]
        )
        name, reason, _ = mod.verdict(checks)
        assert (name, reason) == ("CLAIM", "clean")
        assert mod.EXIT_CODES[name] == 0

    def test_merged_pr_that_did_not_land_is_not_coverage(self, mod):
        """A PR merged into somewhere other than the base is not coverage.

        This is the branch that keeps the check honest in the other direction:
        ``merged`` alone must not park a workable item.
        """
        checks = clean_checks(
            merged_prs=[
                {
                    "number": 7901,
                    "author": "somebody",
                    "is_cross_repository": False,
                    "merge_commit_sha": "deadbeefcafe",
                    "landed": False,
                }
            ]
        )
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("CLAIM", "clean")
        assert evidence == {"risk": "low"}
        assert mod.EXIT_CODES[name] == 0

    def test_open_fork_pr_skips(self, mod):
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8100,
                    "author": "someone",
                    "is_cross_repository": True,
                    "untrusted_fork": False,
                    "closes_item": True,
                }
            ]
        )
        assert naive_claim(checks) is True
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("SKIP", "open-pr")
        assert evidence == {
            "pr": 8100,
            "fork": True,
            "author": "someone",
            "untrusted_fork": False,
        }
        assert mod.EXIT_CODES[name] == 10
        assert (
            mod.human_line(ITEM, name, reason, evidence, "low")
            == f"SKIP {ITEM} open-pr=#8100 fork=true author=someone"
        )

    def test_landed_merged_pr_outranks_an_open_one(self, mod):
        """Precedence 1 before 2: already-fixed is triage debt, not a skip.

        The open hit claims closure, so rule 2 WOULD fire on it. Without that the
        comparison is vacuous -- a mention-only open hit reaches no verdict at
        all, so rule 1 would win by default rather than by rank.
        """
        checks = clean_checks(
            merged_prs=[
                {
                    "number": 7900,
                    "merge_commit_sha": "aaaabbbbcccc",
                    "landed": True,
                    "closes_item": True,
                }
            ],
            open_prs=[
                {
                    "number": 8100,
                    "author": "x",
                    "is_cross_repository": False,
                    "closes_item": True,
                }
            ],
        )
        assert mod.verdict(checks)[:2] == ("CLOSE", "already-fixed")

    def test_prose_self_claim_by_another_user_skips(self, mod):
        checks = clean_checks(
            prose_claim={
                "closure_requested": False,
                "claimed_by_other": True,
                "claimed_by": "otherdev",
                "claimed_by_where": "body",
                "where": "body",
                "comment_id": None,
                "pattern": SELF_CLAIM_SAMPLE_PATTERN,
            }
        )
        assert naive_claim(checks) is True
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("SKIP", "prose-claim")
        assert evidence == {"claimed_by": "otherdev", "where": "body"}
        assert mod.EXIT_CODES[name] == 10
        assert "prose-claim claimed-by=otherdev where=body" in mod.human_line(
            ITEM, name, reason, evidence, "low"
        )

    def test_closure_request_outranks_a_prose_claim(self, mod):
        """Precedence 3 before 4: if the reporter says it is done, that reading
        is the one worth surfacing even when somebody also said they were
        working on it. It surfaces as REVIEW, not CLOSE -- see
        :class:`TestClosureProseNeverCloses`."""
        checks = clean_checks(
            prose_claim={
                "closure_requested": True,
                "claimed_by_other": True,
                "claimed_by": "otherdev",
                "where": "comment",
                "comment_id": 123456,
                "pattern": "x",
            }
        )
        assert naive_claim(checks) is True
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("REVIEW", "reporter-asked-close")
        assert evidence == {"comment_id": 123456, "where": "comment"}
        assert mod.EXIT_CODES[name] == 13
        # The risk is not the caller's to choose here: risk_of() derives it from
        # the same checks the verdict came from, so the line cannot claim a
        # REVIEW is low-risk.
        assert mod.risk_of(checks) == "high"
        assert (
            mod.human_line(ITEM, name, reason, evidence, mod.risk_of(checks))
            == f"REVIEW {ITEM} reporter-asked-close comment-id=123456 risk=high"
        )

    def test_closure_request_in_the_body_reports_where_instead_of_an_id(self, mod):
        evidence = {"comment_id": None, "where": "body"}
        assert (
            mod.human_line(ITEM, "REVIEW", "reporter-asked-close", evidence, "high")
            == f"REVIEW {ITEM} reporter-asked-close where=body risk=high"
        )

    def test_a_claimed_label_with_no_assignee_skips(self, mod):
        """The measured defect, on fabricated checks: another pipeline's
        ``claimed`` label with nobody assigned reads as CLAIM on a live item. A
        label is a claim in its own right -- the skill's own contract calls
        labels and assignees the cross-operator lock -- so it is honoured
        with or without an assignee, the way an open PR is."""
        checks = clean_checks(forge_claim=a_forge_claim("claimed"))
        assert naive_claim(checks) is True  # the old predicate would dispatch
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("SKIP", "forge-claim")
        assert evidence == {"label": "claimed", "assignees": []}
        assert mod.EXIT_CODES[name] == 10
        assert (
            mod.human_line(ITEM, name, reason, evidence, "low")
            == f"SKIP {ITEM} forge-claim label=claimed assignees=none"
        )

    def test_a_foreign_assignee_with_no_label_skips(self, mod):
        """Either field alone is a claim. Measured on this repository, the two
        arrive together from an atomic claim and apart from a human assigning by
        hand, so a rule that needed both would miss half of each."""
        checks = clean_checks(forge_claim=a_forge_claim(None, "otherdev"))
        assert naive_claim(checks) is True
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("SKIP", "forge-claim")
        assert evidence == {"label": None, "assignees": ["otherdev"]}
        assert (
            mod.human_line(ITEM, name, reason, evidence, "low")
            == f"SKIP {ITEM} forge-claim label=none assignees=otherdev"
        )

    def test_the_line_names_every_assignee(self, mod):
        evidence = {"label": "claimed", "assignees": ["otherdev", "boss"]}
        assert (
            mod.human_line(ITEM, "SKIP", "forge-claim", evidence, "low")
            == f"SKIP {ITEM} forge-claim label=claimed assignees=otherdev,boss"
        )

    def test_our_own_claim_is_not_somebody_elses(self, mod):
        """The detector already answered WHOSE claim it is; the verdict only
        reads ``foreign``. A label with ourselves assigned is the shape of our
        own atomic claim, and skipping it would tell a conductor to abandon the
        item it just locked."""
        checks = clean_checks(forge_claim=a_forge_claim("claimed", "us", foreign=False))
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("CLAIM", "clean")
        assert evidence == {"risk": "low"}

    def test_a_landed_merged_pr_outranks_a_forge_claim(self, mod):
        """SKILL.md: an item that is open, claimed and already fixed is triage
        debt, not a work item. The claim must not hide the CLOSE."""
        checks = clean_checks(
            merged_prs=[
                {
                    "number": 7900,
                    "merge_commit_sha": "abc1234def567890",
                    "landed": True,
                    "closes_item": True,
                }
            ],
            forge_claim=a_forge_claim("claimed", "otherdev"),
        )
        assert mod.verdict(checks)[:2] == ("CLOSE", "already-fixed")

    def test_a_covering_open_pr_outranks_a_forge_claim(self, mod):
        """Both SKIP; the reason printed is the stronger evidence, and a PR that
        exists is stronger than a label that says one will."""
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8100,
                    "author": "otherdev",
                    "is_cross_repository": False,
                    "closes_item": True,
                }
            ],
            forge_claim=a_forge_claim("claimed", "otherdev"),
        )
        assert mod.verdict(checks)[:2] == ("SKIP", "open-pr")

    def test_a_closure_request_outranks_a_forge_claim(self, mod):
        """Same rank order as closure-over-prose-claim, for the same reason: if
        the reporter says it is done, a human should hear that before a worker
        -- the claimant's included -- spends a session on it."""
        checks = clean_checks(
            prose_claim={
                "closure_requested": True,
                "claimed_by_other": False,
                "where": "comment",
                "comment_id": 123456,
                "pattern": "x",
            },
            forge_claim=a_forge_claim("claimed", "otherdev"),
        )
        assert mod.verdict(checks)[:2] == ("REVIEW", "reporter-asked-close")

    def test_a_forge_claim_outranks_a_prose_claim(self, mod):
        """Both SKIP. The forge field is the stronger evidence -- it is what the
        skill calls the lock -- so it is the reason that gets printed when the
        same owner also wrote a sentence."""
        checks = clean_checks(
            prose_claim={
                "closure_requested": False,
                "claimed_by_other": True,
                "claimed_by": "otherdev",
                "claimed_by_where": "body",
                "where": "body",
                "comment_id": None,
                "pattern": SELF_CLAIM_SAMPLE_PATTERN,
            },
            forge_claim=a_forge_claim("claimed", "otherdev"),
        )
        assert mod.verdict(checks)[:2] == ("SKIP", "forge-claim")

    def test_a_forge_claim_outranks_an_errored_check(self, mod):
        """A definite answer to the ownership question beats a partial view of
        the symbol question, same as every other positive finding."""
        checks = clean_checks(
            forge_claim=a_forge_claim("claimed"),
            symbol_on_base={"error": "no-clone"},
        )
        assert mod.verdict(checks)[:2] == ("SKIP", "forge-claim")

    def test_a_forge_claim_that_is_not_foreign_never_skips(self, mod):
        """``foreign`` is the only bit the verdict reads. A label with assignees
        that the detector attributed to us must not SKIP however it is spelled."""
        for label, assignees in (("claimed", ["us"]), (None, ["us"]), (None, [])):
            checks = clean_checks(
                forge_claim={"label": label, "assignees": assignees, "foreign": False}
            )
            assert mod.verdict(checks)[0] == "CLAIM", (label, assignees)

    def test_absent_symbol_skips_only_for_a_corroborated_bug_item(self, mod):
        checks = clean_checks(
            symbol_on_base={
                "symbols": ["_merge_notifications", "run_probe"],
                "present": ["run_probe"],
                "missing": ["_merge_notifications"],
                "bug_class": True,
                "bug_class_by": "label:bug",
            }
        )
        assert naive_claim(checks) is True
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("SKIP", "symbol-absent")
        assert evidence == {
            "symbol": "_merge_notifications",
            "missing": ["_merge_notifications"],
            "bug_class_by": "label:bug",
        }
        assert mod.EXIT_CODES[name] == 10
        assert (
            mod.human_line(ITEM, name, reason, evidence, "low")
            == f"SKIP {ITEM} symbol-absent=_merge_notifications"
        )

    def test_an_absent_symbol_on_an_uncorroborated_item_claims_at_high_risk(self, mod):
        """The feature-request case that the unconditional veto parked forever.

        An item proposing to ADD `_merge_notifications` names a symbol that is
        absent by definition, so it would be absent on this pass and every pass
        after it. It must be dispatched, and flagged, not parked.
        """
        checks = clean_checks(
            symbol_on_base={
                "symbols": ["_merge_notifications"],
                "present": [],
                "missing": ["_merge_notifications"],
                "bug_class": False,
                "bug_class_by": None,
            }
        )
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("CLAIM", "clean")
        assert mod.EXIT_CODES[name] == 0
        assert evidence["risk"] == "high"
        assert evidence["symbol_absent_uncorroborated"] == ["_merge_notifications"]
        # The human line stays one field wide, so the reason rides in --json.
        assert mod.human_line(ITEM, name, reason, evidence, "high") == f"CLAIM {ITEM} risk=high"

    def test_an_uncorroborated_absent_symbol_forces_high_risk(self, mod):
        """Even a stale item from a stranger, which recency alone calls low."""
        checks = clean_checks(
            symbol_on_base={
                "symbols": ["Thing_One"],
                "present": [],
                "missing": ["Thing_One"],
                "bug_class": False,
            },
            recency={"age_days": 900, "author_association": "NONE", "risk": "low"},
        )
        assert mod.risk_of(checks) == "high"
        assert mod.uncorroborated_absent_symbols(checks) == ["Thing_One"]

    def test_a_present_symbol_leaves_risk_alone(self, mod):
        checks = clean_checks(
            symbol_on_base={
                "symbols": ["run_probe"],
                "present": ["run_probe"],
                "missing": [],
                "bug_class": False,
            }
        )
        assert mod.uncorroborated_absent_symbols(checks) == []
        assert mod.risk_of(checks) == "low"

    def test_uncorroborated_absent_symbols_ignores_an_errored_check(self, mod):
        checks = clean_checks(symbol_on_base={"error": "grep-failed", "bug_class": False})
        assert mod.uncorroborated_absent_symbols(checks) == []

    @pytest.mark.parametrize(
        "name",
        ["open_prs", "merged_prs", "prose_claim", "symbol_on_base", "recency", "forge_claim"],
    )
    def test_any_errored_check_is_unknown_never_claim(self, mod, name):
        checks = clean_checks(**{name: {"error": "rate-limited"}})
        assert naive_claim(checks) is True
        got, reason, evidence = mod.verdict(checks)
        assert got == "UNKNOWN"
        assert reason == "rate-limited"
        assert evidence == {"check": name, "reason": "rate-limited"}
        assert mod.EXIT_CODES[got] == 3
        assert (
            mod.human_line(ITEM, got, reason, evidence, "high")
            == f"UNKNOWN {ITEM} check={name} reason=rate-limited"
        )

    def test_a_definite_finding_outranks_an_errored_check(self, mod):
        """Precedence 6 sits BELOW the positive findings: a partial view of one
        question does not erase a definite answer to another."""
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8100,
                    "author": "x",
                    "is_cross_repository": False,
                    "closes_item": True,
                }
            ],
            recency={"error": "rate-limited"},
        )
        assert mod.verdict(checks)[:2] == ("SKIP", "open-pr")

    def test_clean_item_claims_and_carries_its_risk(self, mod):
        checks = clean_checks(
            recency={"age_days": 1, "author_association": "CONTRIBUTOR", "risk": "high"}
        )
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("CLAIM", "clean")
        assert evidence == {"risk": "high"}
        assert mod.risk_of(checks) == "high"
        assert mod.EXIT_CODES[name] == 0
        assert mod.human_line(ITEM, name, reason, evidence, "high") == f"CLAIM {ITEM} risk=high"

    def test_the_unreliable_field_is_not_a_check_at_all(self, mod):
        """`closedByPullRequestsReferences` measured `[]` on two items that WERE
        closed by merged PRs, so it is dropped rather than carried as a bonus: a
        per-candidate forge call that cannot change the verdict is pure cost
        against a shared rate limit. Six checks, and none of them is that one.
        """
        assert mod.CHECK_NAMES == (
            "open_prs",
            "merged_prs",
            "prose_claim",
            "symbol_on_base",
            "recency",
            "forge_claim",
        )
        source = SCRIPT.read_text(encoding="utf-8")
        assert "closed_by" not in source
        # The field survives in the rationale only, as the question this script
        # refuses to ask -- never as a call.
        assert "closedByPullRequestsReferences" in source
        assert 'gh_json(\n        [\n            "gh",\n            "issue",' not in source

    def test_no_file_claims_a_check_count_the_code_does_not_have(self, mod):
        """A ratchet, because this exact leftover shipped once.

        Removing one of the checks left the OLD count behind in a docstring and
        in a test name, which a premise-level reviewer correctly reads as the
        description contradicting the diff. Counting words are cheap to forget
        and cheap to pin, so pin them: no line of the script, of this file or of any
        ``*pipeline_conductor_claim_preflight*.py`` file beside it may name a check count
        other than the real one.

        (This docstring deliberately does not spell any wrong count, since the
        scan reads its own file too.)
        """
        wrong = {"five": 5, "six": 6, "four": 4, "seven": 7}
        here = Path(__file__).resolve()
        for path in (SCRIPT, *sorted(here.parent.glob("*pipeline_conductor_claim_preflight*.py"))):
            text = path.read_text(encoding="utf-8")
            for word, count in wrong.items():
                if count == len(mod.CHECK_NAMES):
                    continue
                for line in text.splitlines():
                    lowered = line.lower()
                    if word in lowered and "check" in lowered:
                        raise AssertionError(f"{path.name}: stale check count -- {line.strip()!r}")

    def test_risk_defaults_to_high_when_recency_is_unavailable(self, mod):
        assert mod.risk_of(clean_checks(recency={"error": "rate-limited"})) == "high"
        assert mod.risk_of(clean_checks(recency="nonsense")) == "high"

    def test_entries_and_errored_tolerate_the_error_shape(self, mod):
        assert mod.entries({"error": "x"}) == []
        assert mod.entries([{"a": 1}, "junk"]) == [{"a": 1}]
        assert mod.errored({"error": "x"}) == "x"
        assert mod.errored({}) is None
        assert mod.errored([]) is None


SELF_CLAIM_SAMPLE_PATTERN = r"\bI(?:'m| am)\s+claiming\b"


# --------------------------------------------------------------------------- #
# the prose scanner
# --------------------------------------------------------------------------- #


class TestProseScan:
    def test_self_claim_in_the_body_by_another_user(self, mod):
        issue = an_issue(
            body="I'm claiming this issue, patch coming today.", user={"login": "otherdev"}
        )
        prose = mod.scan_prose(issue, None, "us")
        assert prose["claimed_by_other"] is True
        assert prose["claimed_by"] == "otherdev"
        assert prose["claimed_by_where"] == "body"
        assert prose["closure_requested"] is False
        # The PATTERN is reported, never the user's sentence: this lands in an
        # agent's context.
        assert prose["pattern"] in mod.SELF_CLAIM_RES
        assert "claiming this issue" not in json.dumps(prose)

    def test_our_own_self_claim_is_not_somebody_elses(self, mod):
        issue = an_issue(body="I am claiming this one.", user={"login": "us"})
        assert mod.scan_prose(issue, None, "us")["claimed_by_other"] is False

    def test_an_unknown_identity_reads_a_self_claim_as_somebody_elses(self, mod):
        """Fail-safe direction: not knowing who we are must produce SKIP, not
        CLAIM."""
        issue = an_issue(body="I am claiming this one.", user={"login": "us"})
        assert mod.scan_prose(issue, None, None)["claimed_by_other"] is True

    @pytest.mark.parametrize(
        "text",
        [
            "This is resolved, thanks!",
            "happy to have this closed",
            "Please close this one.",
            "this can be closed",
            "no longer reproducible on main",
        ],
    )
    def test_closure_requests_in_the_last_comment(self, mod, text):
        prose = mod.scan_prose(an_issue(), a_comment(text), "us")
        assert prose["closure_requested"] is True
        assert prose["where"] == "comment"
        assert prose["comment_id"] == 123456

    def test_a_comment_outranks_the_body_as_evidence(self, mod):
        prose = mod.scan_prose(an_issue(body="this is resolved"), a_comment("please close"), "us")
        assert prose["where"] == "comment"
        assert prose["comment_id"] == 123456

    def test_ordinary_prose_claims_nothing(self, mod):
        prose = mod.scan_prose(an_issue(), a_comment("Reproduced on 0.6.0, logs attached."), "us")
        assert prose["closure_requested"] is False
        assert prose["claimed_by_other"] is False

    def test_a_quoted_phrase_is_a_citation_not_a_request(self, mod):
        """The regression that found this: the item SPECIFYING this script quotes
        the closure phrases as a description of what to detect, and a raw scan
        returned CLOSE on a live item. Verbatim from that body — including the
        LINE BREAK inside the quotation, which markdown's hard wrap put there and
        which the first version of the stripper walked straight past.
        """
        body = (
            "- A claim written in **prose** -- an assignee in the body, "
            '"this is\n  resolved, please close" in the last comment -- is '
            "invisible to every label and field query.\n"
            'Scan the body and the last comment for "this is resolved / happy '
            'to have it closed".'
        )
        prose = mod.scan_prose(an_issue(body=body), None, "us")
        assert prose["closure_requested"] is False

    @pytest.mark.parametrize(
        "body",
        [
            "```\nplease close\n```",
            "~~~\nthis is resolved\n~~~",
            "the phrase `please close` fires the check",
            "> this is resolved\n\nbut it is not",
            'they said "please close" and I disagree',
            "they said \u201cplease close\u201d and I disagree",
        ],
    )
    def test_cited_closure_phrases_do_not_request_closure(self, mod, body):
        assert mod.scan_prose(an_issue(body=body), None, "us")["closure_requested"] is False

    @pytest.mark.parametrize(
        "body",
        [
            "This is resolved, thanks for the quick turnaround.",
            "Please close this one.",
            "> irrelevant quoted line\n\nthis was fixed in the 0.7 release",
        ],
    )
    def test_an_unquoted_closure_request_still_fires(self, mod, body):
        """The stripper must not swallow the sentence the check exists to find."""
        assert mod.scan_prose(an_issue(body=body), None, "us")["closure_requested"] is True

    def test_a_cited_self_claim_is_not_a_claim(self, mod):
        body = 'three items said "I am claiming this issue" in prose'
        assert mod.scan_prose(an_issue(body=body), None, "us")["claimed_by_other"] is False

    def test_plain_prose_keeps_unquoted_text(self, mod):
        assert "kept" in mod.plain_prose("kept `dropped` kept")
        assert "dropped" not in mod.plain_prose("kept `dropped` kept")
        assert mod.plain_prose("") == ""

    @pytest.mark.parametrize("pad", [0, 200, 400, 2000])
    def test_a_long_quotation_does_not_escape_the_stripper(self, mod, pad):
        """A length cap on the quotation pattern is a false-CLOSE channel.

        The stripper once bounded the span at 200 characters, so a reporter who
        quoted a closure phrase deep inside a long quotation had it read as
        their OWN prose -- and this verdict performs a write on live work. The
        pad crosses that old bound in both directions, so the test would have
        failed at 400 and 2000 before the bound was removed.
        """
        body = 'The spec says "' + ("context " * (pad // 8)) + 'please close this" -- discuss.'
        assert "please close" not in mod.plain_prose(body)
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False

    @pytest.mark.parametrize(
        "body",
        [
            'he said "this is resolved and I never closed the quote',
            'the doc said "please close this -- anyway, moving on',
            "the doc said \u201cthis is resolved and on it went",
            'ok "happy to have it closed',
        ],
    )
    def test_an_unclosed_quotation_does_not_escape_the_stripper(self, mod, body):
        """The other door into the same false-CLOSE channel, and the one my own
        earlier test missed by asserting ``in (True, False)`` -- which no
        behaviour can fail.

        An unclosed citation matches no balanced pair, so before the fix the
        quoted phrase survived as the author's own words and the verdict was
        CLOSE on live work. Measured, not reasoned: exit 11 on all four bodies.
        """
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False
        name, _, _ = mod.verdict(clean_checks(prose_claim=got))
        assert name == "CLAIM"

    def test_over_stripping_is_the_chosen_direction(self, mod):
        """A leftover delimiter takes the rest of the text with it, so a REAL
        request sitting after an unbalanced quote is lost. That is deliberate:
        losing it costs one dispatch, keeping it can close somebody's work."""
        body = 'he said "something odd -- and separately, this is resolved'
        assert "this is resolved" not in mod.plain_prose(body)
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False

    def test_the_newest_human_comment_is_used_not_the_bot_summary(self, mod):
        """Selection is by timestamp, not position, and bots are skipped.

        Both halves are measured, not assumed. The per-issue comments endpoint
        IGNORES `sort`/`direction` and answers OLDEST-first, so an earlier
        version of this function that asked for `direction=desc` and took the
        first element read the oldest of twelve comments; and this repo's triage
        bot summary was the OLDEST entry, not the newest. The timestamps below
        are that real thread's first and last.
        """
        oldest_first = [
            a_comment(
                "**Automated triage summary**",
                login="github-actions[bot]",
                kind="Bot",
                created="2026-09-01T10:40:23Z",
            ),
            a_comment("reproduced on 0.6.0", ident=111, created="2026-09-01T11:19:53Z"),
            a_comment("some-app[bot] body", login="some-app[bot]", created="2026-09-02T04:22:10Z"),
            a_comment(
                "please close, I fixed this myself", ident=999, created="2026-09-03T00:56:58Z"
            ),
        ]
        found = mod.last_human_comment(oldest_first)
        assert found is not None and found["id"] == 999

    def test_selection_survives_either_page_order(self, mod):
        """The bug this replaces was an ordering ASSUMPTION. Reversing the input
        must not change the answer -- that is what makes the fix a fix rather
        than the opposite assumption.
        """
        page = [
            a_comment("older", ident=111, created="2026-09-01T11:19:53Z"),
            a_comment("newer", ident=999, created="2026-09-03T00:56:58Z"),
        ]
        assert mod.last_human_comment(page)["id"] == 999
        assert mod.last_human_comment(list(reversed(page)))["id"] == 999

    def test_position_breaks_ties_only_when_a_timestamp_is_missing(self, mod):
        undated = [
            a_comment("first", ident=1, created=None),
            a_comment("last", ident=2, created=None),
        ]
        assert mod.last_human_comment(undated)["id"] == 2
        mixed = [
            a_comment("dated", ident=1, created="2026-09-01T00:00:00Z"),
            a_comment("undated", ident=2, created=None),
        ]
        assert mod.last_human_comment(mixed)["id"] == 1

    def test_last_human_comment_degrades_on_junk(self, mod):
        assert mod.last_human_comment([]) is None
        assert mod.last_human_comment("nonsense") is None
        assert mod.last_human_comment([{"user": {"type": "Bot"}}]) is None
        assert mod.last_human_comment(["junk"]) is None
        # A deleted account leaves ``user: null``. That is not a bot, and the
        # sentence it left behind still counts.
        orphaned = mod.last_human_comment([{"id": 5, "user": None}])
        assert orphaned is not None and orphaned["id"] == 5

    def test_a_closure_request_needs_standing(self, mod):
        """CLOSE acts on live work, so "please close" from a passer-by is not
        enough. The reporter and a repository insider both have standing."""
        issue = an_issue(user={"login": "reporter"})
        reporter = a_comment("please close", login="reporter", association="NONE")
        assert mod.scan_prose(issue, reporter, "us")["closure_requested"] is True

        maintainer = a_comment("this was fixed in 0.7", login="boss", association="MEMBER")
        got = mod.scan_prose(issue, maintainer, "us")
        assert got["closure_requested"] is True
        assert got["closure_by"] == "boss"

        passerby = a_comment("please close", login="stranger", association="NONE")
        assert mod.scan_prose(issue, passerby, "us")["closure_requested"] is False

    def test_contributor_association_alone_is_not_standing_to_close(self, mod):
        """CONTRIBUTOR only means "has had a PR merged here once", which is not
        authority over another person's report."""
        drive_by = a_comment("please close", login="stranger", association="CONTRIBUTOR")
        got = mod.scan_prose(an_issue(user={"login": "reporter"}), drive_by, "us")
        assert got["closure_requested"] is False

    def test_a_self_claim_needs_standing_too_but_downgrades_instead(self, mod):
        """Both phrase sets need standing, for OPPOSITE reasons.

        My first version waved self-claims through on the reasoning that SKIP is
        the cheap direction. That was wrong about the cost: a veto anyone can
        cast is a denial-of-work channel -- one comment from a passer-by would
        suppress a queued item indefinitely, and nothing downstream reports the
        suppression. So an unauthorized claim is neither obeyed nor discarded: it
        downgrades to risk=high and takes the live recheck.
        """
        issue = an_issue(user={"login": "reporter"})

        insider = a_comment("I am claiming this one", login="boss", association="MEMBER")
        got = mod.scan_prose(issue, insider, "us")
        assert got["claimed_by_other"] is True
        assert got["claim_without_standing"] is False

        stranger = a_comment("I am claiming this one", login="stranger", association="NONE")
        got = mod.scan_prose(issue, stranger, "us")
        assert got["claimed_by_other"] is False
        assert got["claim_without_standing"] is True
        assert got["claimed_by"] == "stranger"

    def test_a_self_claim_in_the_body_always_has_standing(self, mod):
        """The body's author IS the reporter, so the three items that started
        this -- all of which declared ownership in the BODY -- still SKIP."""
        issue = an_issue(body="Ownership claimed by @otherdev", user={"login": "otherdev"})
        got = mod.scan_prose(issue, None, "us")
        assert got["claimed_by_other"] is True
        assert got["claim_without_standing"] is False


class TestBugClassCorroboration:
    """Explicit metadata only. A label is a human's deliberate triage act, which
    is what makes it corroboration; wording is not."""

    @pytest.mark.parametrize(
        "label,term",
        [
            ("bug", "bug"),
            ("Bug", "bug"),
            ("type: bug", "bug"),
            ("kind/bug", "bug"),
            ("defect", "defect"),
            ("regression", "regression"),
            ("crash", "crash"),
        ],
    )
    def test_a_bug_label_corroborates(self, mod, label, term):
        got, by = mod.bug_class_of(an_issue(labels=[{"name": label}]))
        assert got is True
        # The MATCHED TERM, from this module's own vocabulary -- never the
        # label's text, which is user-authored and this value is printed.
        assert by == f"label:{term}"

    def test_the_corroboration_never_echoes_the_label_text(self, mod):
        """The script's own contract is that nothing user-authored reaches
        stdout. A label name is user-authored, so only the matched term rides."""
        noisy = "type: Bug -- customer ACME, see ticket 4471 (bob@example.invalid)"
        got, by = mod.bug_class_of(an_issue(labels=[{"name": noisy}]))
        assert (got, by) == (True, "label:bug")
        for leaked in ("ACME", "4471", "bob@example.invalid", noisy):
            assert leaked not in str(by)

    @pytest.mark.parametrize(
        "label",
        ["enhancement", "feature request", "documentation", "good first issue", "debugging"],
    )
    def test_a_non_bug_label_does_not_corroborate(self, mod, label):
        assert mod.bug_class_of(an_issue(labels=[{"name": label}])) == (False, None)

    def test_an_issue_type_corroborates(self, mod):
        got, by = mod.bug_class_of(an_issue(type={"name": "Bug"}))
        assert (got, by) == (True, "type:bug")

    def test_a_feature_type_does_not(self, mod):
        assert mod.bug_class_of(an_issue(type={"name": "Feature"})) == (False, None)

    def test_no_metadata_does_not_corroborate(self, mod):
        """An untriaged item is not corroborated, and that is the CHEAP
        direction: it is dispatched with risk=high, not parked."""
        assert mod.bug_class_of(an_issue()) == (False, None)

    def test_junk_metadata_degrades_quietly(self, mod):
        assert mod.bug_class_of(an_issue(labels="nonsense")) == (False, None)
        assert mod.bug_class_of(an_issue(labels=[None, 7, {"no": "name"}])) == (False, None)
        assert mod.bug_class_of(an_issue(type="nonsense")) == (False, None)
        assert mod.bug_class_of(an_issue(labels=["bug"])) == (True, "label:bug")

    def test_the_corroboration_rides_on_the_check_value(self, mod):
        got = mod.symbols_on_base([], None, "main", bug_class=True, bug_class_by="label:bug")
        assert got["bug_class"] is True
        assert got["bug_class_by"] == "label:bug"
        # Also present on the error shapes, so the verdict never reads a missing
        # key as "not a bug".
        assert mod.symbols_on_base(["X_Y"], None, "main", bug_class=True)["bug_class"] is True


class TestForgeClaim:
    """Check 6, the detector: the forge's own ownership fields, read as a claim.

    Measured on live items: four in three days carried another pipeline's
    ``claimed`` label -- two of them with the assignee set too -- and every one
    read CLAIM when nothing here read either field. The queue
    build excludes the label; the live recheck immediately before the atomic
    claim is this script, and it was the one reader that did not look.
    """

    def test_a_claimed_label_alone_is_a_claim(self, mod):
        got = mod.scan_forge_claim(an_issue(labels=[{"name": "claimed"}]), "us")
        assert got == {"label": "claimed", "assignees": [], "foreign": True}

    def test_an_assignee_alone_is_a_claim(self, mod):
        got = mod.scan_forge_claim(an_issue(assignees=[{"login": "otherdev"}]), "us")
        assert got == {"label": None, "assignees": ["otherdev"], "foreign": True}

    def test_our_own_atomic_claim_is_ours(self, mod):
        """Label plus ourselves assigned is exactly what the skill's atomic
        claim writes, so a conductor re-checking an item it holds must not be
        told to skip it."""
        issue = an_issue(labels=[{"name": "claimed"}], assignees=[{"login": "us"}])
        got = mod.scan_forge_claim(issue, "us")
        assert got == {"label": "claimed", "assignees": ["us"], "foreign": False}
        assert mod.scan_forge_claim(an_issue(assignees=[{"login": "us"}]), "us")["foreign"] is False

    def test_a_co_assignee_makes_it_foreign(self, mod):
        """Somebody else is on it too. The fail-safe direction is SKIP."""
        issue = an_issue(assignees=[{"login": "us"}, {"login": "otherdev"}])
        got = mod.scan_forge_claim(issue, "us")
        assert got["assignees"] == ["us", "otherdev"]
        assert got["foreign"] is True

    def test_an_unknown_identity_reads_every_claim_as_foreign(self, mod):
        """Same rule as :func:`scan_prose`: when ``whoami`` could not answer, a
        claim that might be ours still counts as somebody else's."""
        assert mod.scan_forge_claim(an_issue(assignees=[{"login": "us"}]), None)["foreign"] is True
        assert mod.scan_forge_claim(an_issue(labels=[{"name": "claimed"}]), None)["foreign"] is True

    def test_nothing_is_not_a_claim(self, mod):
        got = mod.scan_forge_claim(an_issue(), "us")
        assert got == {"label": None, "assignees": [], "foreign": False}
        assert mod.scan_forge_claim(an_issue(), None)["foreign"] is False

    @pytest.mark.parametrize(
        ("name", "term"),
        [
            ("claimed", "claimed"),
            ("Claimed", "claimed"),
            ("CLAIMED", "claimed"),
            ("in-progress", "in-progress"),
            ("in progress", "in-progress"),
            ("crew: in progress", "in-progress"),
            ("status: In Progress", "in-progress"),
        ],
    )
    def test_the_ownership_vocabulary(self, mod, name, term):
        """The skill's documented ``skip_signals`` default names ``claimed`` and
        ``in-progress``; this repository spells the second ``crew: in progress``
        and applies it with no assignee (measured: 4 of 8 closed items carrying
        it had none), so the assignee rule alone would miss it."""
        assert mod.claim_label_of(an_issue(labels=[{"name": name}])) == term

    @pytest.mark.parametrize(
        "name",
        ["unclaimed", "reclaimed", "bug", "auto-fixable", "needs-human", "progress", "wip"],
    )
    def test_a_label_outside_the_vocabulary_is_not_a_claim(self, mod, name):
        """``\\b`` keeps ``unclaimed`` out. ``wip`` is deliberately NOT in the
        vocabulary: the skill's own default does not list it, and widening a
        veto past the documented contract is how a label nobody meant as a lock
        starts parking items."""
        assert mod.claim_label_of(an_issue(labels=[{"name": name}])) is None

    def test_the_term_is_this_modules_not_the_labels_text(self, mod):
        """Same rule as :func:`bug_class_of`: a label name is user-authored and
        this value is printed, so what comes back is the vocabulary word that
        matched, never the label itself."""
        got = mod.scan_forge_claim(an_issue(labels=[{"name": "Claimed (by the bot!!)"}]), "us")
        assert got["label"] == "claimed"
        line = mod.human_line(
            ITEM, "SKIP", "forge-claim", {"label": got["label"], "assignees": []}, "low"
        )
        assert "by the bot" not in line
        assert line == f"SKIP {ITEM} forge-claim label=claimed assignees=none"

    def test_the_first_matching_label_wins_in_payload_order(self, mod):
        issue = an_issue(labels=[{"name": "crew: in progress"}, {"name": "claimed"}])
        assert mod.claim_label_of(issue) == "in-progress"

    def test_junk_metadata_degrades_quietly(self, mod):
        """Strings where dicts were expected, a singular ``assignee`` where the
        list is missing, nulls everywhere: none of it raises, and only the
        shapes the forge actually sends are read."""
        assert mod.claim_label_of(an_issue(labels=["claimed"])) == "claimed"
        assert mod.claim_label_of(an_issue(labels=[None, 7, {"name": None}])) is None
        assert mod.claim_label_of(an_issue(labels=None)) is None
        assert mod.assignee_logins(an_issue(assignees=None, assignee={"login": "solo"})) == ["solo"]
        assert mod.assignee_logins(an_issue(assignees=[None, {"login": 3}, {"x": 1}])) == []
        assert mod.assignee_logins(an_issue(assignees="otherdev")) == []
        assert mod.assignee_logins({}) == []
        got = mod.scan_forge_claim({"labels": "claimed", "assignees": 42}, "us")
        assert got == {"label": None, "assignees": [], "foreign": False}

    def test_the_pipelines_own_claim_wording_is_not_a_prose_claim(self, mod):
        """A known miss, pinned on purpose. "This issue is now claimed by X's
        instance" -- the sentence every operator's claim comment on this
        repository shares -- matches no self-claim pattern, and this change does
        NOT teach the prose scanner to read it: that comment always lands with
        the label and the assignee it announces, so check 6 is its channel. (The
        newer template's second sentence, "A worker session is picking it up
        now", already fires ``picking it up``; measured here, so the pin is on
        the shared sentence alone.) If a later change adds the phrase, this test
        reds and the question gets asked rather than answered by accident."""
        shared = (
            "Kiro Crew Auto-Pipeline [operator: redacted]: Starting an automated "
            "fix -- this issue is now claimed by redacted's instance."
        )
        assert mod._first_match(mod.SELF_CLAIM_RES, mod.plain_prose(shared)) is None
        newer = shared + " A worker session is picking it up now."
        assert mod._first_match(mod.SELF_CLAIM_RES, mod.plain_prose(newer)) is not None
        # Control: the instrument can match.
        assert mod._first_match(mod.SELF_CLAIM_RES, "I'm claiming this") is not None


class TestClosureNeedsAnIssueAsItsObject:
    """The verb was never the signal; its OBJECT is."""

    @pytest.mark.parametrize(
        "body",
        [
            "Please close the file after reading it, otherwise the handle leaks.",
            "Remember to please close the connection in the finally block.",
            "Can you close the socket before returning?",
            "should we close the stream here",
            "this is resolved by upgrading the pinned dep in your own fork",
        ],
    )
    def test_a_closure_verb_aimed_elsewhere_is_not_a_request(self, mod, body):
        """Measured before this rule existed: every one of these returned
        CLOSE, exit 11, on a live item. They are ordinary sentences in a bug
        report about this kind of software, not requests to close anything."""
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False
        assert mod.verdict(clean_checks(prose_claim=got))[0] == "CLAIM"

    @pytest.mark.parametrize(
        "body",
        [
            "Please close this issue, it is a duplicate.",
            "please close this",
            "Please close.",
            "please close #8029",
            "can this be closed?",
            "should we close the ticket",
            "this is resolved",
            "this was fixed in the 0.7 release",
            "this can be closed",
            "happy to have this closed",
            "no longer an issue",
        ],
    )
    def test_a_real_request_still_fires(self, mod, body):
        """The guard must not buy safety by going deaf: closure detection is
        what saves the dispatch, so each phrasing that names the item is kept."""
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is True

    def test_resolved_by_a_means_differs_from_resolved_as_a_state(self, mod):
        """`in` reports where the fix landed; `by` hands over a workaround and
        leaves the item open. One word apart, opposite verdicts."""
        state = mod.scan_prose(an_issue(body="this is fixed in 0.7"), None, "us")
        means = mod.scan_prose(an_issue(body="this is fixed by pinning it yourself"), None, "us")
        assert state["closure_requested"] is True
        assert means["closure_requested"] is False

    @pytest.mark.parametrize(
        "body",
        [
            "The connection leaks. Please close it.",
            "Please close it, the handle stays open otherwise.",
            "The stream stays open. Please close that.",
            "The socket is still open -- can you close it?",
        ],
    )
    def test_a_bare_pronoun_is_not_an_issue(self, mod, body):
        """A bare pronoun resolves to whatever was last mentioned, and in a bug
        report that is usually a socket, a file or a handle.

        This cost a round to learn: the first version of the object rule
        accepted `it` and `that`, so fixing "please close the file" left "please
        close it" closing live items. Narrowing the object was the fix, not
        widening the clause end.
        """
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False

    @pytest.mark.parametrize(
        "body",
        ["Please close this.", "please close that issue", "please close the item"],
    )
    def test_this_and_an_explicit_noun_still_count(self, mod, body):
        """`this` points at the thread's topic rather than the previous noun, and
        a pronoun WITH the noun present is unambiguous."""
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is True

    def test_happy_to_have_it_closed_is_now_a_deliberate_miss(self, mod):
        """The last pattern that hardcoded a bare pronoun.

        In an issue thread "happy to have it closed" usually does mean the item,
        so this loses a real request -- but the same words describe a socket a
        reporter is happy to see closed, and that reading closed live work. The
        deictic form still fires, which is the phrasing to prefer, and the miss
        costs one dispatch.
        """
        assert (
            mod.scan_prose(an_issue(body="happy to have it closed"), None, "us")[
                "closure_requested"
            ]
            is False
        )
        assert (
            mod.scan_prose(an_issue(body="happy to have this closed"), None, "us")[
                "closure_requested"
            ]
            is True
        )
        assert (
            mod.scan_prose(an_issue(body="happy to have the issue closed"), None, "us")[
                "closure_requested"
            ]
            is True
        )

    def test_a_post_positioned_please_is_a_known_miss(self, mod):
        """ "close the ticket please" is not detected: the pattern is `please
        close`, and the list is hand-written English rather than a parser.

        Pinned rather than fixed, because it fails in the direction this file
        chose everywhere else -- a missed closure request costs one dispatch,
        and widening the verb forms widens the false-CLOSE surface that has now
        produced three separate defects here. If it turns up in real items, the
        evidence is the reason to add it.
        """
        got = mod.scan_prose(an_issue(body="close the ticket please"), None, "us")
        assert got["closure_requested"] is False

    @pytest.mark.parametrize(
        "body",
        [
            "This is fixed-width layout and hard to read",
            "this is fixed-size buffers again",
            "this is fixed-point arithmetic, not floating",
        ],
    )
    def test_an_adjectival_fixed_is_not_a_verdict(self, mod, body):
        r"""``\b`` matches happily before a hyphen, so "this is fixed-width" read
        as "this is fixed" and closed live items. The word is an ADJECTIVE in
        these sentences, and the state patterns now require a continuation that
        a hyphen cannot satisfy."""
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False

    @pytest.mark.parametrize(
        "body",
        [
            "this is resolved",
            "this is resolved, closing",
            "this was fixed in the 0.7 release",
            "this is resolved -- closing now",
            "this is already fixed on main",
        ],
    )
    def test_a_state_statement_still_closes(self, mod, body):
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is True


class TestReferenceRepositoryMustBeKnown:
    """A reference whose repository cannot be established is not coverage."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://api.github.com/repos/other/Repo/issues/12", "other/Repo"),
            ("https://api.github.com/repos/kirodotdev/KiroCrew/pulls/8036", REPO),
            ("https://api.github.com/repos/a/b/issues/1/comments", "a/b"),
            (None, None),
            ("garbage", None),
            ("", None),
            (12, None),
        ],
    )
    def test_the_repo_is_read_from_the_api_url(self, mod, url, expected):
        assert mod.repo_from_api_url(url) == expected

    def test_a_reference_with_no_repository_at_all_is_skipped(self, mod, monkeypatch):
        """The old fallback treated a missing nested repository as "same repo"
        and fetched repos/<this repo>/pulls/<N> -- a DIFFERENT pull request that
        merely shares the number, which then SKIPped a live item. With neither
        the object nor a usable URL, the reference decides nothing."""
        entry = {
            "event": "cross-referenced",
            "source": {"issue": {"number": 999, "pull_request": {}, "url": "garbage"}},
        }
        assert run_main(mod, monkeypatch, Forge(timeline=[entry], pulls={})) == 0

    def test_a_reference_from_another_repo_by_url_is_skipped(self, mod, monkeypatch):
        entry = {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 999,
                    "pull_request": {},
                    "url": "https://api.github.com/repos/somebody/Else/issues/999",
                }
            },
        }
        assert run_main(mod, monkeypatch, Forge(timeline=[entry], pulls={})) == 0

    def test_a_local_reference_identified_only_by_url_is_kept(self, mod, monkeypatch, capsys):
        """The guard must not become deaf to real local references."""
        entry = {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 8100,
                    "pull_request": {},
                    "url": f"https://api.github.com/repos/{REPO}/issues/8100",
                }
            },
        }
        forge = Forge(timeline=[entry], pulls={8100: a_pull(8100)})
        assert run_main(mod, monkeypatch, forge) == 10
        assert "open-pr=#8100" in capsys.readouterr().out


class TestClaimSurvivesLaterChatter:
    """A claim is a statement that holds until withdrawn, not a transcript line
    that the next remark overwrites."""

    def a_comment_at(self, text, login, association, ident, when):
        got = a_comment(text, login=login, association=association, ident=ident)
        got["created_at"] = when
        return got

    def test_a_later_outsider_remark_does_not_erase_an_insider_claim(self, mod):
        """Measured: an insider's claim followed by "any update on this?" left
        the claim unscanned and the verdict CLAIM -- a second worker dispatched
        onto work already in flight, which is the class this script removes."""
        claim = self.a_comment_at(
            "I am claiming this one", "boss", "MEMBER", 1, "2026-09-01T10:00:00Z"
        )
        later = self.a_comment_at(
            "any update on this?", "passerby", "NONE", 2, "2026-09-02T10:00:00Z"
        )
        assert mod.last_human_comment([claim, later])["id"] == 2
        got = mod.newest_authorized_claim([claim, later], "reporter")
        assert got is not None and got["id"] == 1

    def test_the_reporters_own_claim_is_recovered_too(self, mod):
        claim = self.a_comment_at(
            "I am working on this", "reporter", "NONE", 1, "2026-09-01T10:00:00Z"
        )
        later = self.a_comment_at("thanks!", "someone", "NONE", 2, "2026-09-02T10:00:00Z")
        assert mod.newest_authorized_claim([claim, later], "reporter")["id"] == 1

    def test_an_unauthorized_claim_is_not_recovered_into_a_veto(self, mod):
        """The recovery must not become the denial-of-work channel that ruling
        on the prose rule closed: standing is still required here."""
        stranger = self.a_comment_at(
            "I am claiming this", "drifter", "NONE", 3, "2026-09-02T11:00:00Z"
        )
        assert mod.newest_authorized_claim([stranger], "reporter") is None

    def test_the_newest_authorized_claim_wins(self, mod):
        older = self.a_comment_at("I am claiming this", "boss", "MEMBER", 1, "2026-09-01T10:00:00Z")
        newer = self.a_comment_at("I am claiming this", "chief", "OWNER", 2, "2026-09-02T10:00:00Z")
        assert mod.newest_authorized_claim([older, newer], None)["id"] == 2

    def test_a_bot_claim_is_ignored(self, mod):
        bot = self.a_comment_at(
            "I am claiming this", "triage[bot]", "MEMBER", 9, "2026-09-09T00:00:00Z"
        )
        assert mod.newest_authorized_claim([bot], None) is None

    def test_quoted_claims_do_not_count_here_either(self, mod):
        """The recovery reads the same stripped prose as everything else, so a
        comment CITING the phrases this check looks for is not a claim."""
        citing = self.a_comment_at(
            'the script matches "I am claiming this"', "boss", "MEMBER", 1, "2026-09-01T10:00:00Z"
        )
        assert mod.newest_authorized_claim([citing], None) is None

    def test_no_comments_is_not_a_claim(self, mod):
        assert mod.newest_authorized_claim([], None) is None
        assert mod.newest_authorized_claim(None, None) is None


class TestNegatedProseIsNotAVerdict:
    """English puts the negation before the phrase, and these patterns match a
    SUBSTRING, so "I don't think this is resolved" read as "this is resolved"."""

    @pytest.mark.parametrize(
        "body",
        [
            "I don't think this is resolved",
            "I am not sure this is fixed",
            "nobody said this is resolved",
            "this is not resolved yet",
            "I doubt this is resolved",
            "unclear whether this is fixed",
            "I cannot say this is resolved",
            "never said please close this issue",
        ],
    )
    def test_a_negated_closure_phrase_does_not_close(self, mod, body):
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False
        assert mod.verdict(clean_checks(prose_claim=got))[0] == "CLAIM"

    @pytest.mark.parametrize(
        "body",
        ["I am not working on this", "I don't think I am claiming this"],
    )
    def test_a_negated_claim_does_not_park(self, mod, body):
        """Same rule on the claim side. Less destructive than a false CLOSE, but
        a phrase that says the opposite of what it is read as is still wrong."""
        got = mod.scan_prose(an_issue(body=body, user={"login": "other"}), None, "us")
        assert got["claimed_by_other"] is False

    def test_a_nearby_hedge_vetoes_even_a_genuine_later_request(self, mod):
        """A disclosed miss, pinned rather than fixed.

        I first wrote this test expecting "I don't think this is resolved, but
        please close this issue as a duplicate" to close, on the reasoning that
        the scan continues past a negated match. It does continue -- but the
        window is measured in characters, and "don't" sits 35 of them before
        "please close", so the hedge vetoes the later request too.

        That is the direction to fail in: this misses a real request and costs
        one dispatch, where the alternative reading closes live work. Making the
        window clause-aware would need sentence segmentation, which is a bigger
        claim about English than this file should make.
        """
        body = "I don't think this is resolved, but please close this issue as a duplicate"
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False

    def test_a_request_past_the_window_still_fires(self, mod):
        """The veto is bounded: the same request further from the hedge fires."""
        body = "I don't think this is resolved. " + ("More detail here. " * 4) + "please close this"
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is True

    def test_the_negation_window_is_bounded(self, mod):
        """A negation two sentences away must not veto: it is a window, not a
        whole-text search, or any comment containing "not" would go deaf."""
        body = (
            "There is not much documentation here. " + ("padding text. " * 6) + "this is resolved"
        )
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is True


class TestTheBodyIsTheOldestText:
    """A closure request in the body is a status, and a later comment supersedes
    it whether or not that comment contains a phrase this scanner knows."""

    def test_a_newer_comment_retires_a_body_closure_request(self, mod):
        """Measured: body "this is resolved" plus the reporter's later "still
        broken on 0.8" returned CLOSE, exit 11, on work they had just reopened."""
        reopen = a_comment("still broken on 0.8", login="reporter", association="MEMBER", ident=1)
        got = mod.scan_prose(an_issue(body="this is resolved"), reopen, "us")
        assert got["closure_requested"] is False
        assert got["where"] is None

    def test_the_body_still_closes_when_nobody_commented(self, mod):
        """The item that motivated this check had its request in the body, so the
        rule narrows the source rather than removing it."""
        got = mod.scan_prose(an_issue(body="this is resolved"), None, "us")
        assert got["closure_requested"] is True
        assert got["where"] == "body"

    def test_a_comment_can_still_supply_the_request(self, mod):
        ask = a_comment("please close this issue", login="reporter", association="MEMBER", ident=2)
        got = mod.scan_prose(an_issue(body="something broke"), ask, "us")
        assert got["closure_requested"] is True
        assert got["where"] == "comment"

    def test_a_body_claim_survives_a_later_comment(self, mod):
        """Ownership is not a status: the three items that motivated the prose
        check declared ownership in the BODY, so a later remark must not retire
        it the way it retires a closure request."""
        chatter = a_comment("any update?", login="passerby", association="NONE", ident=3)
        issue = an_issue(body="Ownership claimed by @otherdev", user={"login": "otherdev"})
        got = mod.scan_prose(issue, chatter, "us")
        assert got["claimed_by_other"] is True


class TestClaimWithdrawal:
    def a_comment_at(self, text, login, association, ident, when):
        got = a_comment(text, login=login, association=association, ident=ident)
        got["created_at"] = when
        return got

    @pytest.mark.parametrize(
        "text",
        [
            "dropping this, sorry",
            "no longer working on this",
            "not working on this any more",
            "I am off this",
            "unassigning myself",
        ],
    )
    def test_a_newer_withdrawal_retires_an_older_claim(self, mod, text):
        """The cost the first version of the recovery disclosed and accepted --
        an abandoned claim parking the item forever -- did not have to be paid."""
        claim = self.a_comment_at("I am claiming this", "boss", "MEMBER", 1, "2026-09-01T10:00:00Z")
        drop = self.a_comment_at(text, "boss", "MEMBER", 2, "2026-09-02T10:00:00Z")
        assert mod.newest_authorized_claim([claim, drop], "reporter") is None

    def test_a_claim_NEWER_than_the_withdrawal_still_counts(self, mod):
        """Somebody picking the item back up after releasing it owns it again."""
        drop = self.a_comment_at("dropping this", "boss", "MEMBER", 1, "2026-09-01T10:00:00Z")
        claim = self.a_comment_at("I am claiming this", "boss", "MEMBER", 2, "2026-09-02T10:00:00Z")
        got = mod.newest_authorized_claim([drop, claim], "reporter")
        assert got is not None and got["id"] == 2

    def test_a_withdrawal_by_a_bot_is_ignored(self, mod):
        claim = self.a_comment_at("I am claiming this", "boss", "MEMBER", 1, "2026-09-01T10:00:00Z")
        drop = self.a_comment_at(
            "dropping this", "triage[bot]", "MEMBER", 2, "2026-09-02T10:00:00Z"
        )
        assert mod.newest_authorized_claim([claim, drop], "reporter")["id"] == 1

    def test_a_stranger_cannot_release_somebody_elses_claim(self, mod):
        """You can only give up what you hold.

        The first version applied any withdrawal to every earlier claim, so a
        passer-by typing "dropping this" erased a maintainer's claim and the item
        was dispatched on top of live work -- the denial-of-work shape again,
        running the other direction.
        """
        claim = self.a_comment_at("I am claiming this", "boss", "MEMBER", 1, "2026-09-01T10:00:00Z")
        drop = self.a_comment_at("dropping this", "drifter", "NONE", 2, "2026-09-02T10:00:00Z")
        got = mod.newest_authorized_claim([claim, drop], "reporter")
        assert got is not None and got["id"] == 1

    def test_an_insider_cannot_release_another_insiders_claim(self, mod):
        """Standing is authority over the ITEM, not over another person's
        commitment, so it does not transfer here."""
        claim = self.a_comment_at("I am claiming this", "boss", "MEMBER", 1, "2026-09-01T10:00:00Z")
        drop = self.a_comment_at("dropping this", "chief", "OWNER", 2, "2026-09-02T10:00:00Z")
        got = mod.newest_authorized_claim([claim, drop], "reporter")
        assert got is not None and got["id"] == 1

    def test_two_claimants_are_tracked_separately(self, mod):
        """One person leaving must not release the other's claim."""
        gone = self.a_comment_at("I am claiming this", "boss", "MEMBER", 1, "2026-09-01T10:00:00Z")
        drop = self.a_comment_at("dropping this", "boss", "MEMBER", 2, "2026-09-02T10:00:00Z")
        still = self.a_comment_at("I am claiming this", "chief", "OWNER", 3, "2026-09-01T11:00:00Z")
        got = mod.newest_authorized_claim([gone, drop, still], "reporter")
        assert got is not None and got["id"] == 3

    def test_a_body_claim_is_retired_by_its_own_authors_withdrawal(self, mod):
        """The one case the comment path could not reach.

        A claim in the BODY outlives later chatter by design, so before this the
        reporter who claimed an item and then commented "dropping this" left it
        SKIPping forever. That is indefinite suppression, not a wasted dispatch,
        which is the harm this file treats as the serious one. The body predates
        every comment by construction, so any withdrawal from its author retires
        it and no timestamp comparison is needed.
        """
        issue = an_issue(body="Ownership claimed by @otherdev", user={"login": "otherdev"})
        drop = self.a_comment_at("dropping this", "otherdev", "MEMBER", 1, "2026-09-02T10:00:00Z")

        assert mod.body_claim_withdrawn([drop], "otherdev") is True
        assert mod.scan_prose(issue, drop, "us", True)["claimed_by_other"] is False

    def test_a_body_claim_survives_somebody_elses_withdrawal(self, mod):
        issue = an_issue(body="Ownership claimed by @otherdev", user={"login": "otherdev"})
        drop = self.a_comment_at("dropping this", "stranger", "NONE", 1, "2026-09-02T10:00:00Z")

        assert mod.body_claim_withdrawn([drop], "otherdev") is False
        assert mod.scan_prose(issue, drop, "us", False)["claimed_by_other"] is True

    def test_the_withdrawal_map_is_shared_by_both_call_sites(self, mod):
        """One rule, one implementation: the recovery and the body check read the
        same map, so they cannot drift apart."""
        drop = self.a_comment_at("dropping this", "boss", "MEMBER", 1, "2026-09-02T10:00:00Z")
        assert mod.withdrawals_by_author([drop]) == {"boss": "2026-09-02T10:00:00Z"}
        assert mod.withdrawals_by_author([]) == {}
        assert mod.withdrawals_by_author(None) == {}

    def test_an_unknown_reporter_retires_nothing(self, mod):
        drop = self.a_comment_at("dropping this", "boss", "MEMBER", 1, "2026-09-02T10:00:00Z")
        assert mod.body_claim_withdrawn([drop], None) is False


class TestHtmlCommentsAreInvisible:
    """A comment is invisible to every human reader of the item, so nothing
    inside one is a statement its author is making."""

    @pytest.mark.parametrize(
        "body",
        [
            "<!-- please close this issue when done -->\n\nThe button is misaligned.",
            "<!-- this is resolved -->\nStill broken for me.",
            "<!--\nplease close this\n-->\nreal report here",
            "text <!-- this is resolved --> more text",
        ],
    )
    def test_a_phrase_inside_a_comment_does_not_close(self, mod, body):
        """This repository's issue templates ship instructional comments, so
        "<!-- please close this issue when done -->" arrives in the body of real
        items. Reading it closed them."""
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False

    def test_a_real_request_outside_a_comment_still_fires(self, mod):
        body = "<!-- template: describe the bug -->\nplease close this issue"
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is True

    def test_a_comment_is_stripped_before_the_fence_pattern_sees_it(self, mod):
        """Ordering, not just presence: a comment can CONTAIN a fence, a quote or
        a backtick span, and must not be parsed as one."""
        assert mod.plain_prose("<!-- ```this is resolved``` -->\nreal text").strip() == "real text"
        assert "resolved" not in mod.plain_prose('<!-- "this is resolved" -->')

    def test_a_claim_inside_a_comment_does_not_park_the_item(self, mod):
        issue = an_issue(body="<!-- I am claiming this -->", user={"login": "other"})
        assert mod.scan_prose(issue, None, "us")["claimed_by_other"] is False
