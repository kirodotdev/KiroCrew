"""Claim preflight's detectors, each tested on its own.

The guard every closure pattern ships with and the item-scoped closure forms, the
untrusted-fork annotation, closing-reference matching, symbol extraction and the recency
risk. The verdict lattice and the prose scanner live in
``test_pipeline_conductor_claim_preflight.py``; the fixtures they share in
``pipeline_conductor_claim_preflight_helpers.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pipeline_conductor_claim_preflight_helpers import mod_fixture  # noqa: F401
from pipeline_conductor_claim_preflight_helpers import ITEM, REPO, an_issue, clean_checks


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
        """A pattern declared item-scoped but missing from the list would leave the
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
                        "closes_item": True,
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
                    "closes_item": True,
                }
            ]
        )
        assert mod.untrusted_fork_skip(checks) == 8100
        assert mod.risk_of(checks) == "high"
        name, reason, evidence = mod.verdict(checks)
        assert mod.human_line(ITEM, name, reason, evidence, mod.risk_of(checks)) == (
            f"SKIP {ITEM} open-pr=#8100 fork=true author=stranger untrusted-fork=true risk=high"
        )

    def test_a_mention_only_fork_is_not_reported_as_a_suppression(self, mod):
        """The marker names a suppression, so it must not fire where none happens.

        A fork PR that merely MENTIONS the item leaves it in the queue, so calling
        that an untrusted-fork SKIP would name a suppression that does not occur.
        The doubt is not dropped -- the mention path raises the risk on its own --
        but it is reported as what it is.
        """
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8100,
                    "author": "stranger",
                    "is_cross_repository": True,
                    "untrusted_fork": True,
                    "closes_item": False,
                }
            ]
        )
        assert mod.untrusted_fork_skip(checks) is None
        assert mod.mention_only_open_prs(checks) == [8100]
        assert mod.risk_of(checks) == "high"
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("CLAIM", "clean")
        assert evidence["open_pr_mention_only"] == [8100]

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
                    "closes_item": True,
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
        DECIDES the outcome, but only metadata is printed.

        The hit claims closure so this reads the SKIP evidence it is about. A
        mention-only hit would take the CLAIM branch and pass without ever
        building the dict under test.
        """
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8100,
                    "author": "stranger",
                    "is_cross_repository": True,
                    "author_association": "NONE",
                    "untrusted_fork": True,
                    "closes_item": True,
                }
            ]
        )
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("SKIP", "open-pr")
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
        """`#802` must not satisfy a pattern aimed at `8029`, and `80291`
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
