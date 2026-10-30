"""Claim preflight end to end: ``main()`` over a stubbed forge and over a real git.

A partial scan keeping what it confirmed, the read-only guard, every verdict's exit code
and printed line, argument handling, the forge helpers, and the git flags against a real
repository. The verdict lattice over fabricated checks lives in
``test_pipeline_conductor_claim_preflight.py``; the shared fixtures and the stubbed
``Forge`` in ``pipeline_conductor_claim_preflight_helpers.py``.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
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

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS


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


class TestADisclaimedOpenPrDoesNotSuppress:
    """Rule 2 demands a closing keyword, the same thing rule 1 demands.

    The defect these pin: the timeline event rule 2 reads is ``cross-referenced``,
    which is keyword-free by construction -- it fires on a bare mention -- so an
    open PR that said in plain words it was NOT fixing an item took that item out
    of the dispatch queue anyway. ``Refs #N`` is this repository's own idiom for
    referenced-but-deliberately-not-closed and its PR template keeps
    ``Related Issues`` apart from a closing trailer, so the clearest signal an
    author can give that they are leaving an item for somebody else was read as
    the reason to skip it. Silently: the item never appeared as
    refused-for-a-reason, it simply never came up.

    Measured over one real candidate list, of 21 (item, covering PR) pairs 18
    carried a closing keyword and 3 did not, and all 3 of those PRs disclaimed the
    fix in their own words.

    Both directions are here. Declining to suppress is only safe because the
    decline is REPORTED -- a bare reference can still be work in flight whose
    author never spelled a keyword -- so the risk goes high and the item takes the
    live recheck instead of the batch.
    """

    def test_an_open_pr_claiming_closure_is_annotated_and_skips(self, mod, monkeypatch, capsys):
        """The 18-of-21 majority, unchanged. This is the half that must NOT move:
        a PR carrying ``Fixes #N`` is coverage and the item leaves the queue."""
        forge = Forge(timeline=[a_xref(8100)], pulls={8100: a_pull(8100)})
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 10
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "SKIP"
        assert payload["reason"] == "open-pr"
        assert payload["checks"]["open_prs"][0]["closes_item"] is True

    def test_an_open_pr_that_only_mentions_the_item_claims_instead(self, mod, monkeypatch, capsys):
        """The defect, end to end. ``a_pull(closes=None)`` writes
        ``Related to #N`` -- a reference with no closing keyword -- and the item
        is dispatched rather than dropped."""
        forge = Forge(timeline=[a_xref(8100)], pulls={8100: a_pull(8100, closes=None)})
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "CLAIM"
        assert payload["checks"]["open_prs"][0]["closes_item"] is False

    def test_the_declined_suppression_is_reported_not_dropped(self, mod, monkeypatch, capsys):
        """Declining silently would trade one blind spot for another, so the
        finding is published and forces the live recheck."""
        forge = Forge(timeline=[a_xref(8100)], pulls={8100: a_pull(8100, closes=None)})
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["evidence"]["open_pr_mention_only"] == [8100]
        assert payload["risk"] == "high"

    def test_the_repositorys_own_refs_idiom_does_not_suppress(self, mod, monkeypatch, capsys):
        """The sharpest measured case, in the words the author actually used: a PR
        whose only reference is ``Refs #N`` and which says the real fix is a
        follow-up tracked in that very item."""
        disclaiming = dict(
            a_pull(8100, closes=None),
            body=f"Refs #{ITEM} -- the amplifier problem and the follow-up are tracked there.",
        )
        forge = Forge(timeline=[a_xref(8100)], pulls={8100: disclaiming})
        assert run_main(mod, monkeypatch, forge) == 0
        assert f"CLAIM {ITEM} risk=high" in capsys.readouterr().out

    def test_a_closing_pr_is_chosen_over_a_mentioning_one(self, mod, monkeypatch, capsys):
        """Rule 2 reports the PR that actually covers the item, not whichever
        reference the timeline happened to list first. The mentioning PR comes
        first here, so a rule that returned the first hit would name it."""
        forge = Forge(
            timeline=[a_xref(8100), a_xref(8101)],
            pulls={8100: a_pull(8100, closes=None), 8101: a_pull(8101)},
        )
        assert run_main(mod, monkeypatch, forge) == 10
        assert f"SKIP {ITEM} open-pr=#8101" in capsys.readouterr().out

    def test_a_mention_only_hit_is_kept_rather_than_discarded(self, mod):
        """The report needs the number, so the hit stays on the check. A filter
        that dropped it would leave nothing to publish and the decline would be
        as silent as the suppression it replaced."""
        checks = clean_checks(
            open_prs=[
                {
                    "number": 8100,
                    "author": "someone",
                    "is_cross_repository": False,
                    "closes_item": False,
                }
            ]
        )
        assert mod.covering_open_prs(checks) == []
        assert mod.mention_only_open_prs(checks) == [8100]
        name, reason, evidence = mod.verdict(checks)
        assert (name, reason) == ("CLAIM", "clean")
        assert evidence["open_pr_mention_only"] == [8100]
        assert mod.EXIT_CODES[name] == 0


class TestAClosingKeywordCannotSpanTwoFields:
    """A closing reference counts only WITHIN one field.

    The forge honours a closing keyword inside the title or inside the body, never
    assembled across the two. The closing pattern's ``\\s+`` matches a newline, so
    searching a newline-joined ``title + body`` matches a reference NEITHER field
    carries: a title ending ``fix`` glued to a body opening ``#N`` reads as
    ``fix\\n#N``.

    That direction is the dangerous one. It fabricates coverage, and fabricated
    coverage SKIPs an item nobody is fixing -- silently, which is the harm this
    module exists to prevent. The text is arbitrary and opening a fork PR needs no
    permission, so it is craftable rather than accidental.
    """

    @pytest.fixture
    def closing(self, mod):
        return mod.closing_reference_re(REPO, ITEM)

    def test_a_keyword_in_the_title_and_a_reference_in_the_body_is_not_closure(self, mod, closing):
        assert mod.claims_closure(closing, "fix", f"#{ITEM}") is False

    @pytest.mark.parametrize("word", ["fix", "fixes", "closes", "resolved"])
    def test_no_closing_word_reaches_across_the_boundary(self, mod, closing, word):
        """Every member of the keyword vocabulary, not just the one measured."""
        assert mod.claims_closure(closing, word, f"#{ITEM}") is False

    def test_a_real_closure_in_the_body_still_counts(self, mod, closing):
        assert mod.claims_closure(closing, "some title", f"Fixes #{ITEM}") is True

    def test_a_real_closure_in_the_title_still_counts(self, mod, closing):
        """Both fields are searched, so moving the keyword does not lose it."""
        assert mod.claims_closure(closing, f"Fixes #{ITEM}", "body text") is True

    def test_a_bare_reference_in_one_field_is_still_not_closure(self, mod, closing):
        assert mod.claims_closure(closing, "some title", f"Refs #{ITEM}") is False

    @pytest.mark.parametrize("title,body", [(None, None), ("", ""), (None, f"Fixes #{ITEM}")])
    def test_absent_fields_do_not_raise(self, mod, closing, title, body):
        """The forge may omit either field; an absent one is empty, not a crash."""
        assert isinstance(mod.claims_closure(closing, title, body), bool)

    def test_the_fabricated_reference_does_not_skip_the_item(self, mod, monkeypatch, capsys):
        """End to end, through the real verdict: the craftable pair leaves the item
        in the queue and is reported as a mention instead of suppressing it."""
        crafted = dict(a_pull(8100), title="fix", body=f"#{ITEM}")
        forge = Forge(timeline=[a_xref(8100)], pulls={8100: crafted})
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "CLAIM"
        assert payload["checks"]["open_prs"][0]["closes_item"] is False
        assert payload["evidence"]["open_pr_mention_only"] == [8100]

    def test_a_merged_pr_cannot_be_closed_by_a_fabricated_reference_either(
        self, mod, monkeypatch, capsys
    ):
        """The merged path reads the same annotation, so rule 1 is covered by the
        same fix. A fabricated closure there would CLOSE a live item, which is
        stronger than a SKIP and is why the fix belongs at the shared read rather
        than on the open branch alone."""
        crafted = dict(
            a_pull(8100, state="closed", merged=True, sha="a" * 40),
            title="fix",
            body=f"#{ITEM}",
        )
        forge = Forge(timeline=[a_xref(8100)], pulls={8100: crafted})
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "CLAIM"
        assert payload["checks"]["merged_prs"][0]["closes_item"] is False


class TestClosureProseNeverCloses:
    """Rule 3 reads HAND-WRITTEN ENGLISH, and a naive reading answers with the
    strongest thing this script can say about somebody else's live work.

    Nine false-CLOSE paths reached review in one change and each was fixed on the
    merits, but the space of English that accidentally means "close this" has no
    edge, so the next unguarded phrasing was always going to arrive. These tests
    pin the structural answer instead: the reading survives, the response does
    not. They are deliberately split -- one pins the CONSEQUENCE (no dispatch),
    one pins the INTEGER -- because those two can drift apart in a refactor and a
    caller that branches on the number would then break while a
    consequence-only test stayed green.
    """

    def closure_checks(self) -> dict:
        return clean_checks(
            prose_claim={
                "closure_requested": True,
                "claimed_by_other": False,
                "claim_without_standing": False,
                "claimed_by": None,
                "closure_by": "reporter",
                "where": "comment",
                "comment_id": 777,
                "pattern": "x",
            }
        )

    def closure_forge(self) -> Forge:
        return Forge(comments=[a_comment("happy to have this closed", ident=777)])

    def test_a_standing_closure_request_is_never_dispatched(self, mod, monkeypatch):
        """The consequence that matters. A sentence is not permission to spend a
        worker session, and it never was -- this is the half of the old behaviour
        that was right."""
        checks = self.closure_checks()
        assert naive_claim(checks) is True
        name, _, _ = mod.verdict(checks)
        assert name != "CLAIM"
        assert mod.EXIT_CODES[name] != mod.EXIT_CODES["CLAIM"]
        # End to end as well: verdict() is not the only code between the comment
        # and the dispatch, and a test of the pure function alone would not
        # notice collect() or main() losing the reading on the way.
        assert run_main(mod, monkeypatch, self.closure_forge()) != 0

    def test_a_standing_closure_request_is_never_closed_either(self, mod, monkeypatch):
        """The half that was wrong, and the whole point of the change: the item
        must not be CLOSED on prose. Asserting the absence of CLOSE is what
        distinguishes this from the old behaviour, which also refused to
        dispatch."""
        name, _, _ = mod.verdict(self.closure_checks())
        assert name != "CLOSE"
        assert mod.EXIT_CODES[name] != mod.EXIT_CODES["CLOSE"]
        assert run_main(mod, monkeypatch, self.closure_forge()) != mod.EXIT_CODES["CLOSE"]

    def test_a_standing_closure_request_exits_thirteen(self, mod, monkeypatch, capsys):
        """The integer, asserted on its own. The caller branches on this number
        and nothing else, so it is part of the contract rather than an
        implementation detail of the verdict name."""
        name, reason, _ = mod.verdict(self.closure_checks())
        assert (name, reason) == ("REVIEW", "reporter-asked-close")
        assert mod.EXIT_CODES["REVIEW"] == 13
        assert run_main(mod, monkeypatch, self.closure_forge()) == 13
        assert capsys.readouterr().out.strip().startswith("REVIEW")

    def test_a_review_verdict_is_always_high_risk(self, mod):
        """``risk`` is computed by risk_of() from the same checks the verdict came
        from, so the two cannot disagree. Without this, a REVIEW line could print
        ``risk=low`` -- a verdict that exists BECAUSE the evidence is weak,
        reporting that the evidence is fine."""
        checks = self.closure_checks()
        assert mod.verdict(checks)[0] == "REVIEW"
        assert mod.risk_of(checks) == "high"
        assert mod.closure_request(checks) is True

    def test_the_review_line_names_the_comment_to_read(self, mod):
        """This verdict delegates to a human, and it never prints the matched
        sentence, so the identifier that lets them find it is the payload."""
        _, reason, evidence = mod.verdict(self.closure_checks())
        line = mod.human_line(ITEM, "REVIEW", reason, evidence, "high")
        assert "comment-id=777" in line
        assert "risk=high" in line

    def test_a_merged_landed_pr_still_closes(self, mod):
        """The scope boundary. Rule 1 is EVIDENCE -- a merge commit that is an
        ancestor of the base -- not a reading of prose, so it keeps CLOSE.
        Downgrading it too would leave exit 11 with no producers at all."""
        checks = clean_checks(
            merged_prs=[
                {
                    "number": 8712,
                    "landed": True,
                    "closes_item": True,
                    "merge_commit_sha": "c791f0f1",
                }
            ]
        )
        name, reason, _ = mod.verdict(checks)
        assert (name, reason) == ("CLOSE", "already-fixed")
        assert mod.EXIT_CODES[name] == 11

    def test_an_auto_pipeline_stand_down_is_not_read_as_closure_prose(self, mod):
        """A pipeline's own stand-down note is the phrasing that most looks like
        closure without being a reporter's verdict, and it is measurably INERT
        here: it fires no closure pattern and no self-claim pattern.

        Pinned because that is a property somebody could remove by accident. If
        a future change teaches this scanner to read "standing down", this test
        reds and forces the question of whether a pipeline's own note should
        route through REVIEW at all -- rather than the phrase quietly acquiring a
        verdict. It does NOT claim such an item is otherwise protected: this
        script has no check for "my own pipeline already dispositioned this".
        """
        stand_down = (
            "Kiro Crew Auto-Pipeline [operator: redacted] - standing down and "
            "handing back to `needs-human`. No longer being worked by the fleet; "
            "the investigation notes are below for whoever picks it up."
        )
        assert mod._first_match(mod.CLOSURE_RES, mod.plain_prose(stand_down)) is None
        assert mod._first_match(mod.SELF_CLAIM_RES, mod.plain_prose(stand_down)) is None
        # Control: the harness really can match, so the two Nones above are
        # findings and not a silently broken instrument.
        assert mod._first_match(mod.CLOSURE_RES, mod.plain_prose("This is resolved.")) is not None


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

    def test_reporter_asked_close_in_the_last_comment_exits_thirteen(
        self, mod, monkeypatch, capsys
    ):
        forge = Forge(
            comments=[
                a_comment("automated triage summary", login="github-actions[bot]", kind="Bot"),
                a_comment("happy to have this closed", ident=777),
            ]
        )
        assert run_main(mod, monkeypatch, forge) == 13
        assert (
            capsys.readouterr().out.strip()
            == f"REVIEW {ITEM} reporter-asked-close comment-id=777 risk=high"
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

    def test_json_mode_reports_the_six_checks_even_on_unknown(self, mod, monkeypatch, capsys):
        forge = Forge(failures={"/timeline": "API rate limit exceeded"})
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 3
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["checks"]) == set(mod.CHECK_NAMES)
        assert payload["checks"]["open_prs"] == {"error": "rate-limited"}


class TestForgeClaimEndToEnd:
    """Check 6 through ``main``: the issue payload the script already fetched
    answers the ownership question, so the common path costs no extra call."""

    def test_a_claimed_label_with_no_assignee_exits_ten(self, mod, monkeypatch, capsys):
        """The shape as measured live: ``claimed`` for half an hour, no
        assignee, and two CLAIM verdicts in that window."""
        forge = Forge(issue=an_issue(labels=[{"name": "claimed"}]))
        assert run_main(mod, monkeypatch, forge) == 10
        assert (
            capsys.readouterr().out.strip()
            == f"SKIP {ITEM} forge-claim label=claimed assignees=none"
        )
        # No assignee to attribute, so identity is not asked for: the label is
        # foreign whoever we are.
        assert ["gh", "api", "user"] not in forge.calls

    def test_a_claimed_label_with_a_foreign_assignee_exits_ten(self, mod, monkeypatch, capsys):
        """The commoner live shape: label and assignee both set by another
        operator's pipeline. Measured to read CLAIM before this check existed."""
        forge = Forge(
            issue=an_issue(labels=[{"name": "claimed"}], assignees=[{"login": "CrysisDeu"}]),
            login="NicholasRBowers",
        )
        assert run_main(mod, monkeypatch, forge) == 10
        assert (
            capsys.readouterr().out.strip()
            == f"SKIP {ITEM} forge-claim label=claimed assignees=CrysisDeu"
        )

    def test_a_foreign_assignee_with_no_label_exits_ten(self, mod, monkeypatch, capsys):
        forge = Forge(issue=an_issue(assignees=[{"login": "otherdev"}]))
        assert run_main(mod, monkeypatch, forge) == 10
        assert (
            capsys.readouterr().out.strip()
            == f"SKIP {ITEM} forge-claim label=none assignees=otherdev"
        )

    def test_our_own_claim_still_claims(self, mod, monkeypatch, capsys):
        forge = Forge(
            issue=an_issue(labels=[{"name": "claimed"}], assignees=[{"login": "us"}]), login="us"
        )
        assert run_main(mod, monkeypatch, forge) == 0
        assert capsys.readouterr().out.strip() == f"CLAIM {ITEM} risk=low"

    def test_an_unknown_identity_reads_our_assignee_as_foreign(self, mod, monkeypatch, capsys):
        """``gh api user`` failed, so the login that LOOKS like ours cannot be
        confirmed ours. SKIP costs one dispatch; the other reading dispatches
        onto somebody's live work."""
        forge = Forge(issue=an_issue(assignees=[{"login": "us"}]), failures={"user": "boom"})
        assert run_main(mod, monkeypatch, forge) == 10
        assert "forge-claim label=none assignees=us" in capsys.readouterr().out

    def test_a_claimed_item_that_already_landed_still_closes(self, mod, monkeypatch, capsys):
        """SKILL.md: open, claimed and already fixed is triage debt. The forge
        claim sits below rule 1 so the CLOSE is not hidden behind the SKIP."""
        forge = Forge(
            timeline=[a_xref(7900)],
            pulls={7900: a_pull(7900, state="closed", merged=True, sha="abc1234def567890")},
            issue=an_issue(labels=[{"name": "claimed"}], assignees=[{"login": "otherdev"}]),
            git_rc={"merge-base": 0},
        )
        assert run_main(mod, monkeypatch, forge, ["--repo-dir", "/clone"]) == 11
        assert capsys.readouterr().out.strip().startswith(f"CLOSE {ITEM} merged-pr=#7900")

    def test_a_covering_open_pr_is_the_reason_printed(self, mod, monkeypatch, capsys):
        forge = Forge(
            timeline=[a_xref(8100)],
            pulls={8100: a_pull(8100, user="otherdev")},
            issue=an_issue(labels=[{"name": "claimed"}], assignees=[{"login": "otherdev"}]),
        )
        assert run_main(mod, monkeypatch, forge) == 10
        assert capsys.readouterr().out.strip().startswith(f"SKIP {ITEM} open-pr=#8100")

    def test_identity_is_asked_once_when_two_checks_need_it(self, mod, monkeypatch):
        """A foreign assignee AND an insider's prose claim both need to know who
        we are. One forge call answers both; the shared rate limit is why."""
        forge = Forge(
            issue=an_issue(assignees=[{"login": "otherdev"}]),
            comments=[a_comment("I am claiming this issue", login="boss", association="MEMBER")],
        )
        assert run_main(mod, monkeypatch, forge) == 10
        assert forge.calls.count(["gh", "api", "user"]) == 1

    def test_an_unreadable_item_errors_the_check_with_the_others(self, mod, monkeypatch, capsys):
        forge = Forge(failures={f"repos/{REPO}/issues/{ITEM}": "could not resolve host"})
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 3
        payload = json.loads(capsys.readouterr().out)
        assert payload["checks"]["forge_claim"] == {"error": "forge-unreachable"}

    def test_json_mode_carries_the_check_and_the_evidence(self, mod, monkeypatch, capsys):
        forge = Forge(issue=an_issue(labels=[{"name": "crew: in progress"}]))
        assert run_main(mod, monkeypatch, forge, ["--json"]) == 10
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "SKIP"
        assert payload["reason"] == "forge-claim"
        assert payload["checks"]["forge_claim"] == {
            "label": "in-progress",
            "assignees": [],
            "foreign": True,
        }
        assert payload["evidence"] == {"label": "in-progress", "assignees": []}

    def test_a_clean_item_costs_no_identity_call(self, mod, monkeypatch):
        """The common path: no label, no assignee, no prose claim. Nothing here
        may add a forge call to it."""
        forge = Forge()
        assert run_main(mod, monkeypatch, forge) == 0
        assert ["gh", "api", "user"] not in forge.calls


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
        assert mod.EXIT_CODES == {
            "CLAIM": 0,
            "SKIP": 10,
            "CLOSE": 11,
            "REVIEW": 13,
            "UNKNOWN": 3,
        }

    def test_no_two_verdicts_share_an_exit_code(self, mod):
        """The caller branches on the integer alone, so a shared number is a
        wrong branch rather than a confusing message. This is the invariant that
        matters; which particular integers are unallocated is not one, so it is
        deliberately not asserted here."""
        codes = list(mod.EXIT_CODES.values())
        assert len(codes) == len(set(codes))


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

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's own git-env pin, moved verbatim; no test in the test_pipeline_conductor_claim_preflight*.py modules calls monkeypatch.undo()
    def _hermetic_git_for_the_script(self, monkeypatch):
        """The SCRIPT's git inherits this process's environment, unlike the fixture's.

        ``_git`` below hands its spawns ``_fixture_git_env``; ``mod.git`` does not
        take an environment and reads ``os.environ``. Pin the same containment
        there for the duration of each test: the operator's global and system config
        are pointed away and the ``GIT_DIR`` family dropped, so the ancestry and grep
        questions are answered about the clone and nothing else.
        """
        for name in _GIT_LOCATION_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    @pytest.fixture(scope="module")
    def _clone_template(self, tmp_path_factory):
        """Build the landed/sidetrack clone once per module; ``clone`` copies it.

        Six git subprocesses (~2.3-4.7s) would otherwise be paid on every one of the
        8 tests below. Module scope is safe because the template is never handed
        to a test, only copied from via ``shutil.copytree`` -- no test here moves
        a branch or adds a commit to it, they only read commits already present.
        """
        root = tmp_path_factory.mktemp("claim-preflight-seed") / "clone"
        root.mkdir()

        def run_git(*args):
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

    @pytest.fixture
    def clone(self, tmp_path, _clone_template):
        template_root, on_main, off_main = _clone_template
        root = tmp_path / "clone"
        shutil.copytree(template_root, root)
        # A copied checkout reads as "unstaged changes" on Windows (fresh inode/
        # ctime invalidate the index stat cache); nothing in the template is
        # uncommitted, so this changes no content and only re-stats the index.
        rc, _out, err = _git(root, ["reset", "--hard", "HEAD"])
        assert rc == 0, err
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


def _fixture_git_env() -> dict[str, str]:
    """Env for a fixture git call: no host config, templates, hooks, or identity bleed.

    The session/module-scoped template builders below run BEFORE the function-scoped
    ``_git_identity`` autouse fixture in ``test/conftest.py`` has pinned anything, so
    they would otherwise read the developer's real ``~/.gitconfig`` -- a
    ``commit.gpgSign`` aborts the whole template, and a ``core.hooksPath`` or
    ``init.templateDir`` would EXECUTE host hooks from inside the test run. The
    ``GIT_DIR`` location family is dropped (the production list, so an exported
    ``GIT_DIR`` from a hook or ``rebase --exec`` cannot retarget the fixture), both
    template channels are emptied, and identity is supplied. Deliberately NOT
    ``git_command_env()``: that pins ``diff.external`` empty for commands that never
    diff, and these fixtures run ``git diff``.
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env.update(
        {
            "GIT_TEMPLATE_DIR": "",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "init.templateDir",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    # Last, so the entry lands after the GIT_CONFIG_COUNT this helper set.
    env.update(git_maintenance_off_entries(env))
    return env


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
        env=_fixture_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return done.returncode, (done.stdout or "").strip(), (done.stderr or "").strip()
