"""Claim preflight — the conductor's deterministic claim predicate.

Every verdict branch gets a test, and each of those tests also asserts that the
single-question predicate this script replaces would have said CLAIM on the same
item. That second assertion is the point: the three real failures behind the
script (a merged PR an ``--state open`` query cannot see, an open PR the one
field read came back empty for, a claim written in prose) are all shaped the
same way, so a test that only checks the new answer would not notice the script
regressing back into the old blind spot.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
    / "scripts"
    / "claim_preflight.py"
)

REPO = "kirodotdev/KiroCrew"
ITEM = 8029


@pytest.fixture
def mod():
    return load_skill_script("claim_preflight", SCRIPT)


def naive_claim(checks: dict) -> bool:
    """The predicate this script replaces: ONE question, and an empty answer
    read as permission.

    The old skill line was `gh pr list --search ... --state open`, so it could
    see exactly one thing: a same-repo OPEN pull request. It could not see a
    merged PR (wrong state), a fork PR (the search missed them, and the single
    field it fell back on measured empty), a claim written in prose, or a symbol
    that is not on the base.

    Every branch test asserts this returns True, i.e. the old predicate would
    have burned a dispatch on the item.
    """
    hits = checks.get("open_prs")
    if not isinstance(hits, list):
        return True
    return not [hit for hit in hits if isinstance(hit, dict) and not hit.get("is_cross_repository")]


# --------------------------------------------------------------------------- #
# fixtures for fabricated checks
# --------------------------------------------------------------------------- #


def clean_checks(**overrides) -> dict:
    checks = {
        "open_prs": [],
        "merged_prs": [],
        "prose_claim": {
            "closure_requested": False,
            "claimed_by_other": False,
            "claim_without_standing": False,
            "claimed_by": None,
            "where": None,
            "comment_id": None,
            "pattern": None,
        },
        "symbol_on_base": {
            "symbols": [],
            "present": [],
            "missing": [],
            "bug_class": False,
            "bug_class_by": None,
        },
        "recency": {"age_days": 200, "author_association": "NONE", "risk": "low"},
    }
    checks.update(overrides)
    return checks


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
        """Precedence 1 before 2: already-fixed is triage debt, not a skip."""
        checks = clean_checks(
            merged_prs=[
                {
                    "number": 7900,
                    "merge_commit_sha": "aaaabbbbcccc",
                    "landed": True,
                    "closes_item": True,
                }
            ],
            open_prs=[{"number": 8100, "author": "x", "is_cross_repository": False}],
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
        """Precedence 3 before 4: if the reporter says it is done, it is triage
        debt even when somebody also said they were working on it."""
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
        assert (name, reason) == ("CLOSE", "reporter-asked-close")
        assert evidence == {"comment_id": 123456, "where": "comment"}
        assert mod.EXIT_CODES[name] == 11
        assert (
            mod.human_line(ITEM, name, reason, evidence, "low")
            == f"CLOSE {ITEM} reporter-asked-close comment-id=123456"
        )

    def test_closure_request_in_the_body_reports_where_instead_of_an_id(self, mod):
        evidence = {"comment_id": None, "where": "body"}
        assert (
            mod.human_line(ITEM, "CLOSE", "reporter-asked-close", evidence, "low")
            == f"CLOSE {ITEM} reporter-asked-close where=body"
        )

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
        ["open_prs", "merged_prs", "prose_claim", "symbol_on_base", "recency"],
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
            open_prs=[{"number": 8100, "author": "x", "is_cross_repository": False}],
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
        against a shared rate limit. Five checks, and none of them is that one.
        """
        assert mod.CHECK_NAMES == (
            "open_prs",
            "merged_prs",
            "prose_claim",
            "symbol_on_base",
            "recency",
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
        and cheap to pin, so pin them: no line of the script or of this file may
        name a check count other than the real one.

        (This docstring deliberately does not spell any wrong count, since the
        scan reads its own file too.)
        """
        wrong = {"six": 6, "four": 4, "seven": 7}
        for path in (SCRIPT, Path(__file__).resolve()):
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


def an_issue(**overrides) -> dict:
    issue = {
        "number": ITEM,
        "title": "the probe cannot tell a finished worker from a wedged one",
        "body": "The handled set keeps one entry per key.",
        "user": {"login": "reporter"},
        "created_at": "2026-01-01T00:00:00Z",
        "author_association": "NONE",
        "labels": [],
        "type": None,
    }
    issue.update(overrides)
    return issue


def a_comment(
    body: str,
    *,
    login="reporter",
    ident=123456,
    kind="User",
    created="2026-09-02T00:00:00Z",
    association=None,
) -> dict:
    """A comment payload. ``association`` defaults to OWNER only so the many
    tests that just need SOME closure request keep standing; the standing rule
    itself is pinned by its own tests, which pass it explicitly.
    """
    return {
        "id": ident,
        "body": body,
        "user": {"login": login, "type": kind},
        "created_at": created,
        "author_association": "OWNER" if association is None else association,
    }


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


class TestNoClosurePatternShipsUnguarded:
    """The class-level answer to a defect that recurred six times.

    Every round of review found one more closure phrase that matched prose about
    something other than the item -- a file, a socket, a workaround, a layout.
    Each was fixed individually and the next round found another, because nothing
    checked the property itself. These two tests check it.
    """

    RESOURCES = (
        "the file",
        "the socket",
        "the connection",
        "the stream",
        "the handle",
        "the cursor",
        "the session",
        "the channel",
        "the temp file",
    )

    TEMPLATES = (
        "please close {r} after the read",
        "{r} can be closed once we are done",
        "can you close {r} in the finally block",
        "should we close {r} here",
        "happy to have {r} closed by then",
        "{r} is no longer needed",
        "{r} is no longer relevant",
        "this is fixed-width in {r}",
    )

    def test_no_template_about_a_resource_ever_closes(self, mod):
        """The property, stated once: prose about a RESOURCE is not a request to
        close the ITEM. 72 combinations, none may CLOSE."""
        leaks = []
        for resource in self.RESOURCES:
            for template in self.TEMPLATES:
                body = template.format(r=resource)
                got = mod.scan_prose(an_issue(body=body), None, "us")
                if got["closure_requested"]:
                    leaks.append(body)
        assert leaks == []

    def test_every_closure_pattern_carries_a_guard_or_is_declared(self, mod):
        """A ratchet on the LIST, not on a behaviour.

        A new closure phrase added without a guard is the exact mistake that
        shipped six times. Each pattern must either embed one of the shared
        guards or be declared item-scoped with a reason, so the omission fails
        here instead of in review.
        """
        guards = (mod._STATE_END, mod._CLOSE_OBJECT, mod._ISSUE_OBJECT)
        unguarded = [
            pattern
            for pattern in mod.CLOSURE_RES
            if not any(guard in pattern for guard in guards)
            and pattern not in mod.ITEM_SCOPED_CLOSURE_RES
        ]
        assert unguarded == []

    def test_the_declared_set_does_not_drift_from_the_list(self, mod):
        """A pattern declared item-scoped but no longer present would leave the
        ratchet passing over a phrase nobody ships."""
        assert mod.ITEM_SCOPED_CLOSURE_RES <= set(mod.CLOSURE_RES)


class TestTechnicalProseIsNotClosure:
    """The last two patterns without a guard, and the citation form that got
    past the stripper."""

    @pytest.mark.parametrize(
        "body",
        [
            "The temp file can be closed after the read",
            "the workaround is no longer needed",
            "that helper is no longer relevant",
        ],
    )
    def test_prose_about_something_else_does_not_close(self, mod, body):
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False

    @pytest.mark.parametrize(
        "body",
        [
            "this can be closed",
            "this is no longer needed",
            "no longer an issue",
            "no longer reproducible",
        ],
    )
    def test_the_item_scoped_forms_still_close(self, mod, body):
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is True

    @pytest.mark.parametrize("ticks", ["`", "``", "```` "])
    def test_a_backtick_run_of_any_length_is_stripped(self, mod, ticks):
        """Markdown pairs a closing run with the opening one. The single-backtick
        pattern consumed each `` pair as an EMPTY span and left the quoted phrase
        behind as prose, so citing the phrases this scanner looks for closed a
        live item -- the same class as the quotation bug, in code delimiters."""
        run = ticks.strip()
        body = f"the script matches {run}this can be closed{run} in the body"
        assert "this can be closed" not in mod.plain_prose(body)
        got = mod.scan_prose(an_issue(body=body), None, "us")
        assert got["closure_requested"] is False

    def test_real_inline_code_is_still_stripped_not_kept(self, mod):
        assert "dropped" not in mod.plain_prose("kept `dropped` kept")
        assert "kept" in mod.plain_prose("kept `dropped` kept")


class TestAPartialScanKeepsWhatItConfirmed:
    def test_a_confirmed_open_pr_survives_a_later_detail_failure(self, mod, monkeypatch, capsys):
        """A definite finding outranks a partial view, which the module's own
        precedence already says. Discarding the confirmed hit turned a SKIP into
        UNKNOWN over a later PR that could not have changed it."""
        forge = Forge(
            timeline=[a_xref(8100), a_xref(8101)],
            pulls={8100: a_pull(8100)},
            failures={"/pulls/8101": "API rate limit exceeded"},
        )
        assert run_main(mod, monkeypatch, forge) == 10
        assert "open-pr=#8100" in capsys.readouterr().out

    def test_the_merged_side_still_reads_unknown(self, mod, monkeypatch, capsys):
        """The error is not swallowed, only kept off the answer it cannot
        change: the merged check reports it, and rule 1 can still miss a CLOSE
        this way, which costs an item left open rather than a false close."""
        forge = Forge(
            timeline=[a_xref(8100), a_xref(8101)],
            pulls={8100: a_pull(8100)},
            failures={"/pulls/8101": "API rate limit exceeded"},
        )
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 10
        payload = json.loads(capsys.readouterr().out)
        assert payload["checks"]["merged_prs"] == {"error": "rate-limited"}
        assert payload["checks"]["open_prs"][0]["number"] == 8100

    def test_a_failure_before_anything_is_confirmed_is_still_unknown(
        self, mod, monkeypatch, capsys
    ):
        forge = Forge(
            timeline=[a_xref(8101)],
            failures={"/pulls/8101": "API rate limit exceeded"},
        )
        assert run_main(mod, monkeypatch, forge) == 3

    def test_the_fork_marker_survives_the_degraded_path(self, mod, monkeypatch, capsys):
        """The loud suppression has to survive the half-finished scan.

        Keeping a confirmed open PR through a later detail failure created this
        gap: the annotation was gated on there being NO error, so the SKIP came
        out as a routine low-risk one. The marker was therefore missing in
        exactly the conditions that make a suppression hard to notice.
        """
        forge = Forge(
            timeline=[a_xref(8100), a_xref(8101)],
            pulls={8100: a_pull(8100, head="leozhad/KiroCrew", user="leozhad")},
            failures={"/pulls/8101": "API rate limit exceeded"},
        )
        assert run_main(mod, monkeypatch, forge) == 10
        out = capsys.readouterr().out.strip()
        assert "untrusted-fork=true" in out
        assert "risk=high" in out


class TestUntrustedForkAnnotation:
    """The suppression stays; its silence does not.

    A fork PR needs no permission, so rule 2 is a channel anybody can use to
    take an item out of the queue. Refusing to trust fork PRs would cost more
    than the attack does -- most real coverage in a public repo IS a fork PR --
    so the verdict is unchanged and the doubt is published instead.
    """

    def test_a_same_repo_pr_is_never_untrusted(self, mod):
        prs = [{"number": 1, "is_cross_repository": False, "author_association": "NONE"}]
        mod.annotate_untrusted_forks(prs, "reporter")
        assert prs[0]["untrusted_fork"] is False

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_an_insider_fork_is_trusted(self, mod, association):
        prs = [{"number": 2, "is_cross_repository": True, "author_association": association}]
        mod.annotate_untrusted_forks(prs, "reporter")
        assert prs[0]["untrusted_fork"] is False

    @pytest.mark.parametrize("association", ["CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", ""])
    def test_an_outsider_fork_is_untrusted(self, mod, association):
        prs = [
            {
                "number": 3,
                "is_cross_repository": True,
                "author_association": association,
                "author": "stranger",
            }
        ]
        mod.annotate_untrusted_forks(prs, "reporter")
        assert prs[0]["untrusted_fork"] is True

    def test_the_reporters_own_fork_is_trusted(self, mod):
        """Fixing your own bug from a fork is the ordinary case, not an attack."""
        prs = [
            {
                "number": 4,
                "is_cross_repository": True,
                "author_association": "NONE",
                "author": "reporter",
            }
        ]
        mod.annotate_untrusted_forks(prs, "reporter")
        assert prs[0]["untrusted_fork"] is False

    def test_an_unknown_reporter_annotates_more_not_fewer(self, mod):
        """When the item itself could not be read, nobody is known to be the
        reporter -- so the fork is marked, and a marked SKIP only costs a look."""
        prs = [
            {
                "number": 5,
                "is_cross_repository": True,
                "author_association": "NONE",
                "author": "reporter",
            }
        ]
        mod.annotate_untrusted_forks(prs, None)
        assert prs[0]["untrusted_fork"] is True

    def test_the_verdict_does_not_move(self, mod):
        """The whole point: SKIP open-pr either way. If this ever reads CLAIM,
        the duplicate-dispatch class this script was built from is back."""
        for untrusted in (True, False):
            checks = clean_checks(
                open_prs=[
                    {
                        "number": 8100,
                        "author": "stranger",
                        "is_cross_repository": True,
                        "untrusted_fork": untrusted,
                    }
                ]
            )
            name, reason, evidence = mod.verdict(checks)
            assert (name, reason) == ("SKIP", "open-pr")
            assert mod.EXIT_CODES[name] == 10
            assert evidence["untrusted_fork"] is untrusted

    def test_an_untrusted_fork_forces_high_risk_and_says_so(self, mod):
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8100,
                    "author": "stranger",
                    "is_cross_repository": True,
                    "untrusted_fork": True,
                }
            ]
        )
        assert mod.untrusted_fork_skip(checks) == 8100
        assert mod.risk_of(checks) == "high"
        name, reason, evidence = mod.verdict(checks)
        assert mod.human_line(ITEM, name, reason, evidence, mod.risk_of(checks)) == (
            f"SKIP {ITEM} open-pr=#8100 fork=true author=stranger untrusted-fork=true risk=high"
        )

    def test_a_routine_skip_carries_no_marker(self, mod):
        """A marker on every SKIP is noise; this exists so the one that needs a
        look does not read identically to the hundred that do not."""
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8035,
                    "author": "teammate",
                    "is_cross_repository": False,
                    "untrusted_fork": False,
                }
            ]
        )
        assert mod.untrusted_fork_skip(checks) is None
        assert mod.risk_of(checks) == "low"
        name, reason, evidence = mod.verdict(checks)
        line = mod.human_line(ITEM, name, reason, evidence, mod.risk_of(checks))
        assert line == f"SKIP {ITEM} open-pr=#8035 fork=false author=teammate"
        assert "untrusted-fork" not in line

    def test_the_marker_names_no_user_authored_text(self, mod):
        """Same rule as everywhere else here: an association is a forge enum and
        is used to DECIDE, but only metadata is printed."""
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8100,
                    "author": "stranger",
                    "is_cross_repository": True,
                    "author_association": "NONE",
                    "untrusted_fork": True,
                }
            ]
        )
        name, reason, evidence = mod.verdict(checks)
        assert "author_association" not in evidence
        assert "NONE" not in mod.human_line(ITEM, name, reason, evidence, "high")


class TestClosingReference:
    """A merged PR is coverage only if it CLAIMS to close the item."""

    @pytest.mark.parametrize(
        "text",
        [
            "Fixes #8029",
            "fixes #8029",
            "Closes #8029",
            "closed #8029",
            "Resolves #8029",
            "resolve #8029",
            "Fixes: #8029",
            "Fixes kirodotdev/KiroCrew#8029",
            "Fixes https://github.com/kirodotdev/KiroCrew/issues/8029",
            "some prose\n\nFixes #8029\n",
        ],
    )
    def test_a_closing_keyword_is_recognised(self, mod, text):
        assert mod.closing_reference_re(REPO, ITEM).search(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Related to #8029",
            "See #8029",
            "#8029",
            "Fixes #8030",
            "Refs #8029",
            "part of the work for #8029",
            "",
        ],
    )
    def test_a_bare_mention_is_not_a_closing_keyword(self, mod, text):
        assert not mod.closing_reference_re(REPO, ITEM).search(text)

    def test_a_prefix_number_does_not_match(self, mod):
        """`#802` must not satisfy a pattern aimed at `#8029`, and `#80291`
        must not either."""
        assert not mod.closing_reference_re(REPO, 802).search("Fixes #8029")
        assert not mod.closing_reference_re(REPO, ITEM).search("Fixes #80291")

    def test_negation_is_deliberately_invisible(self, mod):
        """Matching GitHub's own parser, which closes the item regardless: this
        script's reading and the forge's reading stay identical."""
        assert mod.closing_reference_re(REPO, ITEM).search("This does not close #8029")


class TestSymbolExtraction:
    @pytest.mark.parametrize(
        "token,expected",
        [
            ("_merge_notifications", True),
            ("PER_FILE_MIN", True),
            ("runProbe", True),
            ("main", False),
            ("true", False),
            ("PR", False),
        ],
    )
    def test_looks_like_symbol(self, mod, token, expected):
        assert mod.looks_like_symbol(token) is expected

    def test_named_symbols_takes_backticked_identifiers_in_order(self, mod):
        text = (
            "`_merge_notifications()` is gone, so `runProbe` and `main` and `_merge_notifications`"
        )
        assert mod.named_symbols(text) == ["_merge_notifications", "runProbe"]

    def test_named_symbols_is_capped(self, mod):
        text = " ".join(f"`sym_{index}`" for index in range(20))
        assert len(mod.named_symbols(text, limit=3)) == 3
        assert mod.named_symbols("") == []


class TestRecency:
    def test_fresh_item_from_an_active_contributor_is_high_risk(self, mod):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        issue = an_issue(
            created_at=(now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            author_association="CONTRIBUTOR",
        )
        got = mod.scan_recency(issue, now=now)
        assert got == {"age_days": 1, "author_association": "CONTRIBUTOR", "risk": "high"}

    def test_old_item_from_an_active_contributor_is_low_risk(self, mod):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        issue = an_issue(created_at="2026-01-01T00:00:00Z", author_association="MEMBER")
        assert mod.scan_recency(issue, now=now)["risk"] == "low"

    def test_fresh_item_from_a_stranger_is_low_risk(self, mod):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        issue = an_issue(
            created_at=(now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            author_association="NONE",
        )
        assert mod.scan_recency(issue, now=now)["risk"] == "low"

    def test_an_unparseable_timestamp_does_not_certify_low_risk(self, mod):
        assert mod.scan_recency(an_issue(created_at="not a date"))["risk"] == "high"
        assert mod.scan_recency(an_issue(created_at=None))["age_days"] is None

    def test_a_naive_timestamp_is_read_as_utc(self, mod):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        assert (
            mod.scan_recency(an_issue(created_at="2026-09-01T00:00:00"), now=now)["age_days"] == 2
        )


# --------------------------------------------------------------------------- #
# the read-only guard — the no-write property, enforced not promised
# --------------------------------------------------------------------------- #


class TestNoWrites:
    @pytest.mark.parametrize(
        "argv",
        [
            ["gh", "api", "repos/o/r/issues/1"],
            ["gh", "api", "repos/o/r/issues/1/timeline", "--paginate"],
            ["gh", "issue", "view", "1", "--repo", "o/r", "--json", "number"],
            ["gh", "api", "user"],
            ["gh", "api", "repos/o/r/pulls/2", "-X", "GET"],
        ],
    )
    def test_reads_are_allowed(self, mod, argv):
        assert mod.is_read_only(argv) is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["gh", "issue", "close", "1"],
            ["gh", "issue", "edit", "1", "--add-label", "claimed"],
            ["gh", "issue", "comment", "1", "--body", "mine"],
            ["gh", "pr", "merge", "2"],
            ["gh", "api", "repos/o/r/issues/1/labels", "-X", "POST"],
            ["gh", "api", "repos/o/r/issues/1", "--method", "PATCH"],
            ["gh", "api", "repos/o/r/issues/1", "--method=DELETE"],
            ["gh", "api", "repos/o/r/issues/1/labels", "-f", "labels[]=claimed"],
            ["gh", "api", "repos/o/r/issues/1", "--field", "state=closed"],
            ["curl", "https://api.github.com"],
            ["gh"],
        ],
    )
    def test_writes_are_refused(self, mod, argv):
        assert mod.is_read_only(argv) is False

    def test_a_refused_argv_never_reaches_a_subprocess(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod, "run", lambda *a, **k: pytest.fail("a refused argv reached subprocess")
        )
        rc, out, err = mod.run_gh(["gh", "issue", "close", "1"])
        assert (rc, out) == (126, "")
        assert "no writes" in err
        assert mod.error_slug(rc, err) == "refused-write"

    def test_the_script_source_contains_no_write_verb(self, mod):
        """A source-level backstop for the guard: a future edit that adds a
        write has to defeat both."""
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "issue close",
            "issue edit",
            "issue comment",
            "pr merge",
            "--add-label",
            "--add-assignee",
            "--remove-label",
        ):
            assert f'"{forbidden}"' not in source
            assert f"'{forbidden}'" not in source

    def test_a_full_run_issues_only_reads(self, mod, monkeypatch):
        forge = Forge()
        monkeypatch.setattr(mod, "run", forge)
        assert mod.main(["--repo", REPO, "--item", str(ITEM)]) == 0
        assert forge.calls, "the run made no calls at all"
        for argv in forge.calls:
            if argv[0] == "gh":
                assert mod.is_read_only(argv), argv
            else:
                # git, and only ever read verbs on a clone we do not own.
                assert argv[:2] == ["git", "-C"]
                assert argv[3] in {"rev-parse", "merge-base", "grep"}, argv


# --------------------------------------------------------------------------- #
# end to end, over a stubbed forge
# --------------------------------------------------------------------------- #


def a_pull(
    number: int,
    *,
    state: str = "open",
    merged: bool = False,
    sha: str | None = None,
    head: str | None = REPO,
    user: str = "someone",
    draft: bool = False,
    closes: int | None = ITEM,
    association: str = "NONE",
) -> dict:
    """A pull payload. ``closes`` puts a closing keyword for that item in the
    body (None for a bare mention), because a merged PR is coverage only if it
    claims to close the item. ``association`` is the author's standing, which
    decides whether a FORK PR's SKIP is marked unvouched."""
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "merge_commit_sha": sha,
        "draft": draft,
        "title": f"fix something in {number}",
        "body": (f"Fixes #{closes}" if closes is not None else f"Related to #{ITEM}"),
        "user": {"login": user},
        "author_association": association,
        "head": {"repo": {"full_name": head} if head else None},
        "base": {"repo": {"full_name": REPO}},
    }


def a_xref(number: int, *, repo: str = REPO) -> dict:
    return {
        "event": "cross-referenced",
        "source": {
            "type": "issue",
            "issue": {
                "number": number,
                "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/{number}"},
                "repository": {"full_name": repo},
            },
        },
    }


class Forge:
    """A stand-in for the module's ``run``: routes an argv to canned output.

    Records every call so a test can assert on what the script asked, including
    that it never asked for a write.
    """

    def __init__(
        self,
        *,
        timeline: list | None = None,
        pulls: dict | None = None,
        issue: dict | None = None,
        comments: list | None = None,
        login: str = "us",
        failures: dict | None = None,
        git_rc: dict | None = None,
    ):
        self.timeline = timeline if timeline is not None else []
        self.pulls = pulls or {}
        self.issue = issue if issue is not None else an_issue()
        self.comments = comments if comments is not None else []
        self.login = login
        self.failures = failures or {}
        self.git_rc = git_rc or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        if argv[0] == "git":
            # ``rev-parse --git-dir`` is the --repo-dir validation and
            # ``rev-parse --verify`` the default-branch check: two different
            # questions, so a test can fail one without failing the other.
            verb = "git-dir" if "--git-dir" in argv else argv[3]
            return self.git_rc.get(verb, 0), "", ""
        target = argv[2] if len(argv) > 2 else ""
        for needle, slug in self.failures.items():
            if needle in " ".join(argv):
                return 1, "", slug
        if "/timeline" in target:
            return 0, json.dumps(self.timeline), ""
        if "/pulls/" in target:
            number = int(target.rsplit("/", 1)[1])
            return 0, json.dumps(self.pulls[number]), ""
        if "/comments" in target:
            return 0, json.dumps(self.comments), ""
        if target == "user":
            return 0, json.dumps({"login": self.login}), ""
        if target.startswith("repos/") and "/issues/" in target:
            return 0, json.dumps(self.issue), ""
        raise AssertionError(f"unrouted argv: {argv}")  # pragma: no cover


def run_main(mod, monkeypatch, forge: Forge, extra: list[str] | None = None) -> int:
    monkeypatch.setattr(mod, "run", forge)
    return mod.main(["--repo", REPO, "--item", str(ITEM), *(extra or [])])


class TestEndToEnd:
    def test_clean_item_exits_zero_and_prints_the_claim_line(self, mod, monkeypatch, capsys):
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        forge = Forge(issue=an_issue(created_at=fresh, author_association="CONTRIBUTOR"))
        assert run_main(mod, monkeypatch, forge) == 0
        assert capsys.readouterr().out.strip() == f"CLAIM {ITEM} risk=high"

    def test_a_merged_landed_pr_exits_eleven(self, mod, monkeypatch, capsys):
        forge = Forge(
            timeline=[a_xref(7900)],
            pulls={7900: a_pull(7900, state="closed", merged=True, sha="abc1234def567890")},
            git_rc={"merge-base": 0},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 11
        assert (
            capsys.readouterr().out.strip()
            == f"CLOSE {ITEM} merged-pr=#7900 sha=abc1234def landed=true"
        )

    def test_a_merged_pr_that_landed_elsewhere_still_claims(self, mod, monkeypatch, capsys):
        forge = Forge(
            timeline=[a_xref(7901)],
            pulls={7901: a_pull(7901, state="closed", merged=True, sha="deadbeefcafe")},
            git_rc={"merge-base": 1},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 0
        assert capsys.readouterr().out.strip().startswith(f"CLAIM {ITEM}")

    def test_a_merged_pr_that_only_mentions_the_item_still_claims(self, mod, monkeypatch, capsys):
        """End to end for the mention path: landed on the base, but the body
        only says "Related to", so it is not coverage."""
        forge = Forge(
            timeline=[a_xref(7902)],
            pulls={7902: a_pull(7902, state="closed", merged=True, sha="abc1234def", closes=None)},
            git_rc={"merge-base": 0},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 0
        assert capsys.readouterr().out.strip().startswith(f"CLAIM {ITEM}")

    def test_an_unauthorized_claim_claims_at_high_risk(self, mod, monkeypatch, capsys):
        """A SKIP any commenter can cast is a denial-of-work channel, so an
        unauthorized claim annotates instead of parking."""
        forge = Forge(
            comments=[
                a_comment(
                    "I am claiming this issue",
                    login="stranger",
                    association="NONE",
                    ident=555,
                )
            ]
        )
        assert run_main(mod, monkeypatch, forge) == 0
        assert capsys.readouterr().out.strip() == f"CLAIM {ITEM} risk=high"

    def test_an_insider_claim_still_skips(self, mod, monkeypatch, capsys):
        forge = Forge(
            comments=[
                a_comment("I am claiming this issue", login="boss", association="MEMBER", ident=556)
            ]
        )
        assert run_main(mod, monkeypatch, forge) == 10
        out = capsys.readouterr().out.strip()
        assert out.startswith(f"SKIP {ITEM} prose-claim claimed-by=boss")

    def test_a_buried_insider_claim_still_skips(self, mod, monkeypatch, capsys):
        """End to end for the recovery, which is the half a unit test on the
        selector cannot show: the newest comment is the passer-by's, so the
        claim is found only because ownership gets its own lookup."""
        claim = a_comment("I am claiming this issue", login="boss", association="MEMBER", ident=1)
        claim["created_at"] = "2026-09-01T10:00:00Z"
        later = a_comment("any update on this?", login="passerby", association="NONE", ident=2)
        later["created_at"] = "2026-09-02T10:00:00Z"

        assert run_main(mod, monkeypatch, Forge(comments=[claim, later])) == 10
        assert (
            capsys.readouterr().out.strip().startswith(f"SKIP {ITEM} prose-claim claimed-by=boss")
        )

    def test_a_buried_unauthorized_claim_does_not_skip(self, mod, monkeypatch, capsys):
        """The recovery must not resurrect a veto anybody can cast."""
        claim = a_comment("I am claiming this issue", login="drifter", association="NONE", ident=1)
        claim["created_at"] = "2026-09-01T10:00:00Z"
        later = a_comment("any update on this?", login="passerby", association="NONE", ident=2)
        later["created_at"] = "2026-09-02T10:00:00Z"

        assert run_main(mod, monkeypatch, Forge(comments=[claim, later])) == 0
        assert capsys.readouterr().out.strip() == f"CLAIM {ITEM} risk=low"

    def test_an_open_fork_pr_exits_ten(self, mod, monkeypatch, capsys):
        """An outsider's fork PR still SKIPs -- and now says it is unvouched, so
        the suppression reaches the conductor instead of only the queue."""
        forge = Forge(
            timeline=[a_xref(8100)],
            pulls={8100: a_pull(8100, head="leozhad/KiroCrew", user="leozhad")},
        )
        assert run_main(mod, monkeypatch, forge) == 10
        assert capsys.readouterr().out.strip() == (
            f"SKIP {ITEM} open-pr=#8100 fork=true author=leozhad untrusted-fork=true risk=high"
        )

    def test_an_insiders_fork_pr_skips_quietly(self, mod, monkeypatch, capsys):
        """A maintainer working from a fork is not a suppression risk, so the
        marker stays off and the line is the routine one."""
        forge = Forge(
            timeline=[a_xref(8100)],
            pulls={
                8100: a_pull(8100, head="leozhad/KiroCrew", user="leozhad", association="MEMBER")
            },
        )
        assert run_main(mod, monkeypatch, forge) == 10
        out = capsys.readouterr().out.strip()
        assert out == f"SKIP {ITEM} open-pr=#8100 fork=true author=leozhad"
        assert "untrusted-fork" not in out

    def test_a_deleted_head_repo_reads_as_a_fork(self, mod, monkeypatch, capsys):
        forge = Forge(timeline=[a_xref(8101)], pulls={8101: a_pull(8101, head=None)})
        assert run_main(mod, monkeypatch, forge) == 10
        assert "fork=true" in capsys.readouterr().out

    def test_a_closed_unmerged_pr_frees_the_item(self, mod, monkeypatch):
        forge = Forge(
            timeline=[a_xref(8102)], pulls={8102: a_pull(8102, state="closed", merged=False)}
        )
        assert run_main(mod, monkeypatch, forge) == 0

    def test_a_reference_from_another_repository_is_not_coverage(self, mod, monkeypatch):
        forge = Forge(timeline=[a_xref(5, repo="someone/else")], pulls={})
        assert run_main(mod, monkeypatch, forge) == 0

    def test_the_other_timeline_events_are_ignored(self, mod, monkeypatch):
        """A real timeline is mostly not cross-references. Measured on one item
        of this repo: 11 ``commented``, 7 ``labeled``, 2 ``referenced``, 5
        ``cross-referenced`` — and one of those five pointed at an ISSUE, not a
        PR. Every shape but the PR cross-reference must pass through untouched.
        """
        issue_to_issue = a_xref(4242)
        issue_to_issue["source"]["issue"].pop("pull_request")
        forge = Forge(
            timeline=[
                {"event": "commented", "body": "still broken"},
                {"event": "labeled", "label": {"name": "bug"}},
                {"event": "referenced", "commit_id": "abc"},
                issue_to_issue,
                {"event": "cross-referenced", "source": None},
                {"event": "cross-referenced", "source": {"issue": "junk"}},
                "not even a dict",
                a_xref(8100),
                a_xref(8100),  # the same PR twice: one detail call, not two
            ],
            pulls={8100: a_pull(8100, user="someone")},
        )
        assert run_main(mod, monkeypatch, forge) == 10
        detail_calls = [c for c in forge.calls if len(c) > 2 and "/pulls/" in c[2]]
        assert detail_calls == [["gh", "api", f"repos/{REPO}/pulls/8100"]]

    def test_a_failure_detailing_one_pr_is_unknown(self, mod, monkeypatch, capsys):
        """The reference exists but its fork-ness and merge commit are unknown:
        a half-read coverage answer must not read as "no coverage"."""
        forge = Forge(
            timeline=[a_xref(8100)],
            pulls={8100: a_pull(8100)},
            failures={"/pulls/8100": "API rate limit exceeded"},
        )
        assert run_main(mod, monkeypatch, forge) == 3
        assert "check=open_prs reason=rate-limited" in capsys.readouterr().out

    def test_unparseable_pr_detail_is_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(timeline=[a_xref(8100)], pulls={8100: ["not", "an", "object"]})
        assert run_main(mod, monkeypatch, forge) == 3
        assert "reason=unparseable-json" in capsys.readouterr().out

    def test_prose_claim_by_another_user_exits_ten(self, mod, monkeypatch, capsys):
        forge = Forge(
            issue=an_issue(body="Ownership claimed by @otherdev", user={"login": "otherdev"}),
            login="us",
        )
        assert run_main(mod, monkeypatch, forge) == 10
        out = capsys.readouterr().out.strip()
        assert out == f"SKIP {ITEM} prose-claim claimed-by=otherdev where=body"

    def test_reporter_asked_close_in_the_last_comment_exits_eleven(self, mod, monkeypatch, capsys):
        forge = Forge(
            comments=[
                a_comment("automated triage summary", login="github-actions[bot]", kind="Bot"),
                a_comment("happy to have this closed", ident=777),
            ]
        )
        assert run_main(mod, monkeypatch, forge) == 11
        assert (
            capsys.readouterr().out.strip() == f"CLOSE {ITEM} reporter-asked-close comment-id=777"
        )

    def test_an_absent_symbol_on_a_labelled_bug_exits_ten(self, mod, monkeypatch, capsys):
        forge = Forge(
            issue=an_issue(
                body="`_merge_notifications` never fires.",
                labels=[{"name": "bug"}],
            ),
            git_rc={"grep": 1},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 10
        assert capsys.readouterr().out.strip() == f"SKIP {ITEM} symbol-absent=_merge_notifications"

    def test_an_absent_symbol_on_an_unlabelled_item_claims_at_high_risk(
        self, mod, monkeypatch, capsys
    ):
        """End to end for the item class the unconditional veto parked: nothing
        corroborates bug-class, so it is dispatched and flagged, not parked."""
        forge = Forge(
            issue=an_issue(body="Please add `_merge_notifications` so the digest can batch."),
            git_rc={"grep": 1},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 0
        assert capsys.readouterr().out.strip() == f"CLAIM {ITEM} risk=high"

    def test_an_issue_type_corroborates_bug_class_too(self, mod, monkeypatch):
        forge = Forge(
            issue=an_issue(
                body="`_merge_notifications` never fires.",
                type={"name": "Bug"},
            ),
            git_rc={"grep": 1},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 10

    def test_a_present_symbol_claims(self, mod, monkeypatch):
        forge = Forge(issue=an_issue(body="`_merge_notifications` misfires."), git_rc={"grep": 0})
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 0

    def test_a_named_symbol_without_a_clone_is_unknown(self, mod, monkeypatch, capsys):
        """The honest half of ``--repo-dir`` being optional: a question git alone
        can answer, with no git, is UNKNOWN — not a guess in either direction."""
        forge = Forge(issue=an_issue(body="`_merge_notifications` never fires."))
        assert run_main(mod, monkeypatch, forge) == 3
        assert "check=symbol_on_base reason=no-repo-dir" in capsys.readouterr().out

    def test_a_merged_pr_without_a_clone_is_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(
            timeline=[a_xref(7900)],
            pulls={7900: a_pull(7900, state="closed", merged=True, sha="abc1234def")},
        )
        assert run_main(mod, monkeypatch, forge) == 3
        assert "check=merged_prs reason=no-repo-dir" in capsys.readouterr().out

    def test_an_unresolvable_ancestry_is_unknown_not_did_not_land(self, mod, monkeypatch, capsys):
        """A stale clone missing the merge commit must not read as "did not
        land" — that reading is exactly how an already-fixed item was
        dispatched."""
        forge = Forge(
            timeline=[a_xref(7900)],
            pulls={7900: a_pull(7900, state="closed", merged=True, sha="abc1234def")},
            git_rc={"merge-base": 128},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 3
        assert "reason=ancestry-unknown" in capsys.readouterr().out

    def test_a_merged_pr_with_no_merge_commit_is_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(
            timeline=[a_xref(7900)],
            pulls={7900: a_pull(7900, state="closed", merged=True, sha=None)},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 3
        assert "reason=no-merge-commit" in capsys.readouterr().out

    def test_an_unknown_default_branch_is_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(
            timeline=[a_xref(7900)],
            pulls={7900: a_pull(7900, state="closed", merged=True, sha="abc1234def")},
            git_rc={"rev-parse": 1},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 3
        assert "reason=unknown-default-branch" in capsys.readouterr().out

    def test_a_failed_grep_is_unknown_not_an_absent_symbol(self, mod, monkeypatch, capsys):
        forge = Forge(
            issue=an_issue(body="`_merge_notifications` never fires."), git_rc={"grep": 2}
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 3
        assert "reason=grep-failed" in capsys.readouterr().out

    def test_a_rate_limited_timeline_is_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(failures={"/timeline": "API rate limit exceeded"})
        assert run_main(mod, monkeypatch, forge) == 3
        assert (
            capsys.readouterr().out.strip() == f"UNKNOWN {ITEM} check=open_prs reason=rate-limited"
        )

    def test_an_unreachable_issue_endpoint_is_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(failures={f"repos/{REPO}/issues/{ITEM}": "could not resolve host"})
        assert run_main(mod, monkeypatch, forge) == 3
        assert "reason=forge-unreachable" in capsys.readouterr().out

    def test_a_failed_comment_fetch_is_unknown_not_half_an_answer(self, mod, monkeypatch, capsys):
        forge = Forge(failures={"/comments": "server error"})
        assert run_main(mod, monkeypatch, forge) == 3
        assert "check=prose_claim" in capsys.readouterr().out

    def test_the_dropped_check_costs_no_forge_call(self, mod, monkeypatch):
        """The reason it was dropped: a call that cannot change the verdict is
        pure cost against a shared rate limit. Assert the call is not made."""
        forge = Forge()
        assert run_main(mod, monkeypatch, forge) == 0
        for argv in forge.calls:
            assert "closedByPullRequestsReferences" not in " ".join(argv)
            assert argv[:2] != ["gh", "issue"]

    def test_too_many_references_is_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(timeline=[a_xref(n) for n in range(9000, 9000 + mod.MAX_PR_DETAILS + 1)])
        assert run_main(mod, monkeypatch, forge) == 3
        assert "reason=too-many-references" in capsys.readouterr().out

    def test_unparseable_forge_output_is_unknown(self, mod, monkeypatch):
        forge = Forge()

        def broken(argv, cwd=None):
            forge.calls.append(list(argv))
            if "/timeline" in " ".join(argv):
                return 0, "{not json", ""
            return forge(argv, cwd)

        monkeypatch.setattr(mod, "run", broken)
        assert mod.main(["--repo", REPO, "--item", str(ITEM)]) == 3

    def test_json_mode_prints_exactly_one_object(self, mod, monkeypatch, capsys):
        forge = Forge(
            timeline=[a_xref(8100)],
            pulls={8100: a_pull(8100, head="leozhad/KiroCrew", user="leozhad")},
        )
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 10
        out = capsys.readouterr().out
        payload = json.loads(out)  # one object, or this raises
        assert out.strip().count("\n") == 0
        assert payload["item"] == ITEM
        assert payload["verdict"] == "SKIP"
        assert payload["reason"] == "open-pr"
        assert payload["risk"] in {"low", "high"}
        assert set(payload["checks"]) == set(mod.CHECK_NAMES)
        assert payload["evidence"] == {
            "pr": 8100,
            "fork": True,
            "author": "leozhad",
            "untrusted_fork": True,
        }
        # The machine form and the human form must agree about the doubt.
        assert payload["risk"] == "high"

    def test_json_mode_reports_the_five_checks_even_on_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(failures={"/timeline": "API rate limit exceeded"})
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 3
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["checks"]) == set(mod.CHECK_NAMES)
        assert payload["checks"]["open_prs"] == {"error": "rate-limited"}


class TestArgumentHandling:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--repo", "not-a-repo", "--item", "1"],
            ["--repo", REPO, "--item", "0"],
            ["--repo", REPO, "--item", "-3"],
            ["--repo", REPO, "--item", "1", "--default-branch", "  "],
        ],
    )
    def test_malformed_arguments_exit_two(self, mod, monkeypatch, argv, capsys):
        monkeypatch.setattr(mod, "run", lambda *a, **k: pytest.fail("ran before validating"))
        assert mod.main(argv) == 2
        assert capsys.readouterr().err.strip().startswith("malformed")

    def test_a_repo_dir_that_is_not_a_clone_exits_two(self, mod, monkeypatch, capsys):
        monkeypatch.setattr(mod, "run", lambda argv, cwd=None: (128, "", "not a git repository"))
        assert mod.main(["--repo", REPO, "--item", "1", "--repo-dir", "/nope"]) == 2
        assert "not a git repository" in capsys.readouterr().err

    def test_a_non_numeric_item_is_rejected_by_argparse(self, mod):
        with pytest.raises(SystemExit) as excinfo:
            mod.main(["--repo", REPO, "--item", "eight"])
        assert excinfo.value.code == 2

    def test_exit_codes_are_the_documented_ones(self, mod):
        assert mod.EXIT_CODES == {"CLAIM": 0, "SKIP": 10, "CLOSE": 11, "UNKNOWN": 3}


class TestForgeHelpers:
    def test_run_reports_a_missing_binary_instead_of_raising(self, mod):
        rc, out, err = mod.run(["definitely-not-a-real-binary-9f3a"])
        assert rc == 127
        assert out == ""
        assert mod.error_slug(rc, err) == "gh-missing"

    @pytest.mark.parametrize(
        "rc,err,slug",
        [
            (1, "API rate limit exceeded for user", "rate-limited"),
            (1, "HTTP 429 Too Many Requests", "rate-limited"),
            (1, "gh auth login required", "not-authenticated"),
            (1, "HTTP 404: Not Found", "not-found"),
            (1, "dial tcp: lookup api.github.com", "forge-unreachable"),
            (1, "context deadline exceeded: timed out", "forge-unreachable"),
            (7, "something else entirely", "gh-error-rc7"),
        ],
    )
    def test_error_slugs_never_echo_the_stderr(self, mod, rc, err, slug):
        got = mod.error_slug(rc, err)
        assert got == slug
        assert " " not in got

    def test_gh_json_parses_and_reports(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "run", lambda argv, cwd=None: (0, '{"a": 1}', ""))
        assert mod.gh_json(["gh", "api", "user"]) == ({"a": 1}, None)
        monkeypatch.setattr(mod, "run", lambda argv, cwd=None: (0, "", ""))
        assert mod.gh_json(["gh", "api", "user"]) == (None, None)
        monkeypatch.setattr(mod, "run", lambda argv, cwd=None: (0, "{", ""))
        assert mod.gh_json(["gh", "api", "user"]) == (None, "unparseable-json")

    def test_pagination_survives_either_output_shape(self, mod):
        """`gh api --paginate` MERGES array pages into one document -- measured on
        gh 2.96.0, a 4-page timeline came back as one array of 33. This pins that
        path AND the concatenated-documents shape, because the script pins no gh
        version and a page boundary must never turn a covered item into UNKNOWN.
        """
        merged = mod.parse_pages('[{"n": 1}, {"n": 2}, {"n": 3}]')
        assert merged == [{"n": 1}, {"n": 2}, {"n": 3}]

        concatenated = mod.parse_pages('[{"n": 1}, {"n": 2}]\n[{"n": 3}]\n')
        assert concatenated == [{"n": 1}, {"n": 2}, {"n": 3}]

        # A single object, and several of them, both survive.
        assert mod.parse_pages('{"login": "us"}') == {"login": "us"}
        assert mod.parse_pages('{"a": 1}\n{"b": 2}') == [{"a": 1}, {"b": 2}]
        assert mod.parse_pages("null") is None

    def test_a_torn_page_is_still_a_failed_answer(self, mod, monkeypatch):
        """Tolerating page boundaries must not tolerate garbage: an unparseable
        payload stays UNKNOWN rather than becoming an empty result."""
        with pytest.raises(ValueError):
            mod.parse_pages('[{"n": 1}] [{"n": 2}')
        monkeypatch.setattr(mod, "run", lambda argv, cwd=None: (0, '[{"n": 1}] [{"n": 2}', ""))
        assert mod.gh_json(["gh", "api", "x"]) == (None, "unparseable-json")

    def test_a_multi_page_timeline_still_finds_its_references(self, mod, monkeypatch):
        """End to end over the concatenated shape: the covering PR is found, so a
        page boundary cannot produce a false CLAIM."""
        pages = json.dumps([a_xref(8100)]) + "\n" + json.dumps([a_xref(8101)])
        forge = Forge(
            pulls={
                8100: a_pull(8100, state="closed", merged=False),
                8101: a_pull(8101, user="someone"),
            }
        )

        def paged(argv, cwd=None):
            forge.calls.append(list(argv))
            if len(argv) > 2 and "/timeline" in argv[2]:
                return 0, pages, ""
            return forge(argv, cwd)

        monkeypatch.setattr(mod, "run", paged)
        assert mod.main(["--repo", REPO, "--item", str(ITEM)]) == 10

    def test_whoami_degrades_to_none(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "run", lambda argv, cwd=None: (0, '{"login": "us"}', ""))
        assert mod.whoami() == "us"
        monkeypatch.setattr(mod, "run", lambda argv, cwd=None: (1, "", "boom"))
        assert mod.whoami() is None
        monkeypatch.setattr(mod, "run", lambda argv, cwd=None: (0, "[]", ""))
        assert mod.whoami() is None


class TestAgainstRealGit:
    """The git flags, over a real repository.

    Everything else stubs ``run``, which cannot catch a wrong flag: a bad
    ``git grep`` invocation returns a non-zero code that the stub would never
    produce, and ``--is-ancestor`` reversed would read every landed merge as
    unlanded. One small real repo pins both.
    """

    @pytest.fixture
    def clone(self, tmp_path):
        root = tmp_path / "clone"
        root.mkdir()

        def run_git(*args):
            rc, out, err = 0, "", ""
            rc, out, err = _git(root, list(args))
            assert rc == 0, f"git {args} failed: {err}"
            return out

        run_git("init", "--initial-branch=main", "-q")
        run_git("config", "user.email", "preflight@example.invalid")
        run_git("config", "user.name", "preflight")
        (root / "landed.py").write_text("def _merge_notifications():\n    pass\n", encoding="utf-8")
        run_git("add", "landed.py")
        run_git("commit", "-q", "-m", "landed")
        on_main = run_git("rev-parse", "HEAD")
        run_git("checkout", "-q", "-b", "sidetrack")
        (root / "elsewhere.py").write_text("SIDE = 1\n", encoding="utf-8")
        run_git("add", "elsewhere.py")
        run_git("commit", "-q", "-m", "elsewhere")
        off_main = run_git("rev-parse", "HEAD")
        run_git("checkout", "-q", "main")
        return root, on_main, off_main

    def test_ancestry_distinguishes_landed_from_elsewhere(self, mod, clone):
        root, on_main, off_main = clone
        merged = [
            {"number": 1, "merge_commit_sha": on_main, "closes_item": True},
            {"number": 2, "merge_commit_sha": off_main, "closes_item": True},
        ]
        assert mod.annotate_landed(merged, str(root), "main") is None
        assert merged[0]["landed"] is True
        assert merged[1]["landed"] is False

    def test_a_commit_absent_from_the_clone_is_not_did_not_land(self, mod, clone):
        root, _, _ = clone
        merged = [{"number": 3, "merge_commit_sha": "0" * 40, "closes_item": True}]
        assert mod.annotate_landed(merged, str(root), "main") == "ancestry-unknown"
        assert "landed" not in merged[0]

    def test_an_unknown_branch_is_reported(self, mod, clone):
        root, on_main, _ = clone
        merged = [{"number": 1, "merge_commit_sha": on_main, "closes_item": True}]
        assert mod.annotate_landed(merged, str(root), "no-such-branch") == "unknown-default-branch"

    def test_a_mention_only_merge_is_never_asked_about_ancestry(self, mod, clone):
        """A merged PR that only MENTIONS the item cannot reach rule 1 whatever
        its ancestry, so an unanswerable question about it must not turn the
        whole check UNKNOWN. An error is for a question whose answer matters."""
        root, _, _ = clone
        mention = [{"number": 4, "merge_commit_sha": "0" * 40, "closes_item": False}]
        assert mod.annotate_landed(mention, str(root), "main") is None
        assert mod.annotate_landed(mention, None, "main") is None
        assert mod.annotate_landed(mention, str(root), "no-such-branch") is None
        assert "landed" not in mention[0]

    def test_a_claiming_merge_alongside_a_mention_is_still_asked(self, mod, clone):
        """The scoping must not become a way to skip the check that matters."""
        root, on_main, _ = clone
        merged = [
            {"number": 4, "merge_commit_sha": "0" * 40, "closes_item": False},
            {"number": 5, "merge_commit_sha": on_main, "closes_item": True},
        ]
        assert mod.annotate_landed(merged, str(root), "main") is None
        assert merged[1]["landed"] is True
        assert "landed" not in merged[0]

    def test_no_merged_prs_needs_no_clone(self, mod):
        assert mod.annotate_landed([], None, "main") is None

    def test_git_grep_finds_a_symbol_on_the_branch_only(self, mod, clone):
        root, _, _ = clone
        got = mod.symbols_on_base(["_merge_notifications", "SIDE"], str(root), "main")
        assert got["present"] == ["_merge_notifications"]
        assert got["missing"] == ["SIDE"]
        assert mod.symbols_on_base(["SIDE"], str(root), "sidetrack")["present"] == ["SIDE"]

    def test_symbols_on_base_needs_no_clone_when_nothing_is_named(self, mod):
        assert mod.symbols_on_base([], None, "main") == {
            "symbols": [],
            "present": [],
            "missing": [],
            "bug_class": False,
            "bug_class_by": None,
        }

    def test_symbols_on_base_reports_an_unknown_branch(self, mod, clone):
        root, _, _ = clone
        got = mod.symbols_on_base(["SIDE"], str(root), "no-such-branch")
        assert got["error"] == "unknown-default-branch"


def _git(root: Path, args: list[str]):
    """Run git against ``root``, contained two ways.

    ``-C root`` already makes git run as if started there, so the operation
    targets the fixture. ``cwd=root`` is belt and braces: it means a future
    call site that forgets ``-C`` still cannot reach the checkout, which is
    what the no-test-side-effects rule is protecting.
    """
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(root), *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return done.returncode, (done.stdout or "").strip(), (done.stderr or "").strip()
