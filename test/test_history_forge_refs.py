"""Tests for forge references as history search queries.

Covers pull-request, merge-request and issue numbers in every spelling a
transcript uses (a ``#`` sigil, a pasted link, a typed ``pr`` reference), their
digit boundaries, ranking hints and expansion budget, and the seam through which a
registered source provider contributes its own id spellings. Plain content search
is in ``test_history_session_search.py``.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew import history, history_search
from kiro_crew.history import ConversationLog


class TestForgeReferenceSearch:
    """Pull-request / merge-request / issue numbers as first-class queries.

    A transcript names the same pull request several ways — a ``#`` sigil in prose,
    a pasted ``…/pull/`` link, or a typed ``pr`` reference. A
    literal-substring query finds only the spelling the searcher happened to
    guess, so these pin the alternation, its digit boundary, and the ranking
    hint a bare number gets.
    """

    @staticmethod
    def _needle(query: str) -> history.SearchNeedle:
        needles, _, _ = history.parse_search_query(query)
        required = [n for n in needles if n.required]
        assert len(required) == 1, f"{query!r} must gate on one reference: {required}"
        return required[0]

    @staticmethod
    def _corpus(tmp_path) -> ConversationLog:
        log = ConversationLog(base_dir=tmp_path)
        log.append(
            "url_only",
            "assistant",
            "opened https://github.com/kirodotdev/KiroCrew/pull/4411 for app sync",
        )
        log.append("hash_form", "assistant", "babysitting PR #4411 to green")
        log.append("prose_form", "assistant", "rebased pr 4411 onto main")
        log.append("longer_number", "assistant", "opened /pull/44110 as a follow-up")
        log.append("digit_noise", "assistant", "the run id was 1544110293 and it timed out")
        log.append(
            "gitlab_mr",
            "assistant",
            "see https://gitlab.com/grp/proj/-/merge_requests/12 for the fix",
        )
        return log

    @pytest.mark.parametrize(
        "query",
        [
            "#4411",
            "PR #4411",
            "PR 4411",
            "pr4411",
            "pull/4411",
            "https://github.com/kirodotdev/KiroCrew/pull/4411",
            "https://github.com/kirodotdev/KiroCrew/pull/4411/files",
            "kirodotdev/KiroCrew#4411",
            "(#4411)",
            "#4411.",
        ],
        ids=[
            "sigil",
            "word-sigil",
            "word-number",
            "glued",
            "path",
            "url",
            "url-subpath",
            "repo-sigil",
            "parenthesized",
            "trailing-period",
        ],
    )
    def test_every_spelling_finds_every_spelling(self, tmp_path, query):
        """The named defect: one reference, one result set, whatever form is typed."""
        log = self._corpus(tmp_path)

        keys = {s["key"] for s in log.search_sessions(query, 10)}

        assert keys == {"url_only", "hash_form", "prose_form"}, query

    def test_digit_boundary_excludes_a_longer_number(self, tmp_path):
        """A ``#``-number query is not a prefix search — a longer number is a different PR."""
        log = self._corpus(tmp_path)

        assert [s["key"] for s in log.search_sessions("#4411", 10)] != []
        assert "longer_number" not in {s["key"] for s in log.search_sessions("#4411", 10)}
        assert {s["key"] for s in log.search_sessions("#44110", 10)} == {"longer_number"}

    def test_digit_boundary_excludes_digits_inside_a_run_id(self, tmp_path):
        """A reference query means the item, not the digits: a run id is not a PR."""
        log = self._corpus(tmp_path)

        assert "digit_noise" not in {s["key"] for s in log.search_sessions("#4411", 10)}

    def test_naming_word_is_dropped_from_the_gate(self):
        """Requiring the literal "pr" would disqualify a URL-only transcript.

        The word introduces the number; it is not part of the reference.
        """
        needle = self._needle("PR 4411")

        assert needle.text == "#4411"
        assert {"pull/4411", "4411"} <= set(needle.alts)

    def test_merge_request_family_is_separate(self, tmp_path):
        """GitLab numbers merge requests apart from issues, so ``!12`` != ``#12``."""
        log = self._corpus(tmp_path)
        log.append("gh_issue", "assistant", "filed #12 against the parser")

        assert {s["key"] for s in log.search_sessions("!12", 10)} == {"gitlab_mr"}
        assert {s["key"] for s in log.search_sessions("MR !12", 10)} == {"gitlab_mr"}
        assert {s["key"] for s in log.search_sessions("#12", 10)} == {"gh_issue"}

    def test_gitlab_url_parses_as_a_merge_request(self):
        needle = self._needle("https://gitlab.com/grp/proj/-/merge_requests/12")

        assert needle.text == "!12"
        assert "merge_requests/12" in needle.alts

    def test_bare_number_keeps_plain_substring_recall(self, tmp_path):
        """Numeric content search is untouched — a bare number is not a reference.

        Rewriting every number into a reference would silently break searching
        for a port, an error code or a run id.
        """
        log = self._corpus(tmp_path)

        keys = {s["key"] for s in log.search_sessions("4411", 10)}

        assert {"digit_noise", "longer_number"} <= keys, "plain digits still match"

    def test_bare_number_ranks_the_real_reference_first(self, tmp_path):
        """The ranking half: same recall, but the PR session comes first."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("noise", "assistant", "run id 1544110293 aborted, retried 1544110293 twice")
        log.append("the_pr", "assistant", "reviewed #4411 and pushed")

        keys = [s["key"] for s in log.search_sessions("4411", 10)]

        assert keys[0] == "the_pr", f"reference must outrank digit noise: {keys}"

    def test_bare_number_ranking_needle_cannot_gate(self):
        """The spellings are scoring-only and NOT adjacency evidence.

        Counting them as adjacency evidence would arm the CJK adjacency floor,
        turning a ranking hint into a hidden gate that drops every session
        matching the digits but not a reference.
        """
        needles, _, floor = history.parse_search_query("4411")

        scoring = [n for n in needles if not n.required]
        assert [n.text for n in needles if n.required] == ["4411"]
        assert scoring and all(not n.adjacency for n in scoring)
        assert floor is False

    def test_repo_slug_breaks_a_cross_repo_tie(self, tmp_path):
        """Same number in two repos: the one the query named ranks first.

        The named-repo session is written FIRST, so it is the older of the two
        and the recency boost works AGAINST it — only the repo needle can lift
        it above the other. Written the other way round, recency alone would
        order the rows correctly and the test would pass with no repo needle at
        all.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("named_repo", "assistant", "kirodotdev/kirocrew#4411 needed a rebase")
        log.append("other_repo", "assistant", "looked at #4411 in the vendor tree")

        keys = [
            s["key"]
            for s in log.search_sessions("https://github.com/kirodotdev/kirocrew/pull/4411", 10)
        ]

        assert keys[0] == "named_repo", keys

    def test_a_sigil_captured_repo_also_ranks(self, tmp_path):
        """The repo slug is captured from `owner/repo#N` too, not only from a URL."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("named_repo", "assistant", "kirodotdev/kirocrew#4411 needed a rebase")
        log.append("other_repo", "assistant", "looked at #4411 in the vendor tree")

        keys = [s["key"] for s in log.search_sessions("kirodotdev/kirocrew#4411", 10)]

        assert keys[0] == "named_repo", keys

    def test_reference_expansions_are_bounded(self):
        """Each expansion costs several scans per session, so the count is capped."""
        over_cap = history._SEARCH_MAX_FORGE_REFS + 2
        query = " ".join(f"#{i}" for i in range(100, 100 + over_cap))

        needles, _, _ = history.parse_search_query(query)

        expanded = [n for n in needles if n.alts]
        assert len(expanded) == history._SEARCH_MAX_FORGE_REFS
        plain = [n for n in needles if n.required and not n.alts]
        assert plain, "tokens past the cap degrade to plain needles, never vanish"

    def test_snippet_centers_on_the_spelling_present(self, tmp_path):
        """The hit may be spelled unlike the query, so alts are snippet anchors."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("url_only", "assistant", "x " * 200 + "merged /pull/4411 today" + " y" * 200)

        results = log.search_sessions("#4411", 10)

        assert results[0]["snippet"], "a content hit must produce a snippet"
        assert "pull/4411" in results[0]["snippet"]

    def test_a_spelling_that_is_already_required_does_not_also_score(self):
        """A number and its ``#`` sigil names one item twice — its hits must not count twice.

        Order matters and this is the load-bearing one: with the bare number
        FIRST a ranking hint is created before the sigil makes it redundant, so
        the end-of-parse cleanup is what removes it. Sigil-first never creates the
        hint at all, so that order cannot prove the cleanup works.
        """
        needles, _, _ = history.parse_search_query("4411 #4411")

        assert sorted(n.text for n in needles if n.required) == ["#4411", "4411"]
        assert [n for n in needles if not n.required] == []

    @pytest.mark.parametrize("query", ["4411 !4411", "!4411 4411"])
    def test_a_cross_family_repeat_does_not_score_a_required_spelling(self, query):
        """\"4411 !4411" gates the GitLab family; the hint must not re-score it.

        The bare number's ranking hint is keyed on the GitHub spelling, so the
        by-key sweep leaves it alone — but its alts carry BOTH families, and the
        GitLab ones are exactly what the sigil made required. Every required
        spelling (keys and the alts required needles carry) must be purged from
        the hint, in either token order; the hint still ranks on the GitHub
        spellings that survive.
        """
        needles, _, _ = history.parse_search_query(query)

        required_spellings = {s for n in needles if n.required for s in (n.text, *n.alts)}
        hints = [n for n in needles if not n.required]
        assert hints, "the hint must survive on its remaining family"
        for hint in hints:
            assert not ({hint.text, *hint.alts} & required_spellings)
        # The surviving hint still carries the OTHER family's spellings, at the
        # forge weight, digit-bounded, and NOT as adjacency evidence — the
        # rebuild must preserve the flags, not only the spellings.
        assert any("#4411" in (n.text, *n.alts) for n in hints)
        for hint in hints:
            assert hint.weight == history_search._FORGE_REF_WEIGHT
            assert hint.digit_bounded and not hint.adjacency

    def test_a_provider_that_gates_every_hint_spelling_drops_the_hint(self, monkeypatch):
        """A hint left with NO spelling of its own contributes nothing and goes.

        Built-in spellings alone cannot empty a hint — its canonical form is
        only ever a required KEY, which the by-key sweep already removes — but
        a registered provider's alts can blanket the remainder. The purge must
        drop the whole entry rather than emit a needle with zero effective
        spellings.
        """
        gh = history_search._forge_spellings(history_search._ForgeRef("4411", False, None))
        mr = history_search._forge_spellings(history_search._ForgeRef("4411", True, None))
        spellings = {
            "acme-a4411": (gh[0], *gh[1]),
            "acme-b4411": (mr[0], *mr[1]),
        }
        monkeypatch.setattr(
            history_search,
            "_search_ref_resolver",
            lambda token: (token, spellings[token]) if token in spellings else None,
        )

        needles, _, _ = history.parse_search_query("acme-a4411 acme-b4411 4411")

        assert [n for n in needles if not n.required] == []
        # The bare number still gates as a plain literal term.
        assert any(n.required and n.text == "4411" for n in needles)

    def test_a_single_form_query_keeps_its_ranking_hint(self):
        """The purge must not touch a hint whose item was named only once."""
        needles, _, _ = history.parse_search_query("4411")

        hints = [n for n in needles if not n.required]
        assert len(hints) == 1
        assert "#4411" in (hints[0].text, *hints[0].alts)
        assert "!4411" in (hints[0].text, *hints[0].alts)

    def test_a_glued_mr_token_is_the_gitlab_family(self):
        """The glued form carries no sigil, so its WORD names the family."""
        assert self._needle("mr-12").text == "!12"
        assert self._needle("mr12").text == "!12"
        assert self._needle("pr-12").text == "#12"

    def test_a_sigil_query_finds_prose_and_glued_transcripts(self, tmp_path):
        """The transcript may never use a sigil, and the query should not care.

        A session whose only mention is "pull request 4411" or "pr4411" is about
        the item the query named; requiring one of the sigil/path spellings left
        it unreachable.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("prose_long", "assistant", "opened pull request 4411 this morning")
        log.append("glued", "assistant", "pr4411 needs a rebase")
        log.append("mr_prose", "assistant", "merge request 12 was approved")
        log.append("mr_glued", "assistant", "mr12 is the mirror of it")

        assert {s["key"] for s in log.search_sessions("#4411", 10)} == {
            "prose_long",
            "glued",
        }
        assert {s["key"] for s in log.search_sessions("!12", 10)} == {
            "mr_prose",
            "mr_glued",
        }

    def test_prose_spellings_keep_the_digit_boundary(self, tmp_path):
        """The added spellings end in digits, so they must not prefix-match.

        Both families, since each carries its own prose and glued form.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("longer", "assistant", "opened pull request 44110 and pr44110")
        log.append("longer_mr", "assistant", "merge request 120 and mr120 landed")

        assert log.search_sessions("#4411", 10) == []
        assert log.search_sessions("!12", 10) == []

    def test_naming_one_item_twice_never_narrows_it(self, tmp_path):
        """\"#42 issue 42" must find everything either spelling finds alone.

        Skipping outright in the dedup path keeps `issue` required and throws
        away the bare-digit spelling the sigil-free occurrence contributes
        — narrowing a query that named the item MORE ways, which the loosen-only
        contract forbids.
        """
        needles, _, _ = history.parse_search_query("#42 issue 42")

        required = [n for n in needles if n.required]
        assert [n.text for n in required] == ["#42"], required
        assert "42" in required[0].alts

        log = ConversationLog(base_dir=tmp_path)
        log.append("path_only", "assistant", "reviewed pull/42 today")
        log.append("prose_only", "assistant", "we hit issue 42 in prod")

        keys = {s["key"] for s in log.search_sessions("#42 issue 42", 10)}

        assert keys == {"path_only", "prose_only"}, keys

    def test_one_item_named_two_ways_charges_one_budget_slot(self):
        """A sigil and a bare spelling of one number are one item, not two.

        Charging both spent a slot on a ranking hint the parse then discarded as
        redundant, which could push a later distinct reference past the cap.
        Asserted in both orders, since a ledger keyed by item is what makes the
        outcome independent of which form the user typed first.
        """
        for query in ("#4411 4411 #5 #6", "4411 #4411 #5 #6"):
            needles, _, _ = history.parse_search_query(query)
            expanded = {n.text for n in needles if n.alts}
            assert expanded == {"#4411", "#5", "#6"}, (query, expanded)

    def test_the_ranking_hint_carries_both_families(self, tmp_path):
        """A bare number ranks a GitLab mention too, not only a GitHub one.

        The hint exists because a bare number cannot know which forge it means;
        pinning only the GitHub half would let the GitLab spellings rot.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("noise", "assistant", "took 12 ms, then 12 ms again, then 12 ms")
        log.append("the_mr", "assistant", "reviewed !12 today")

        assert [s["key"] for s in log.search_sessions("12", 10)][0] == "the_mr"

    def test_a_repeated_reference_does_not_spend_two_budget_slots(self):
        """The budget counts distinct references, not tokens.

        Charging per token let "#1 #1 #2 #3" spend two slots on #1 and push #3
        past the cap, where it degraded to a plain `#3` needle and stopped
        matching the item's other spellings (a URL mention of PR 3).
        """
        needles, _, _ = history.parse_search_query("#1 #1 #2 #3")

        expanded = {n.text for n in needles if n.alts}
        assert expanded == {"#1", "#2", "#3"}, expanded

    def test_a_repeated_bare_number_does_not_spend_two_budget_slots(self):
        """Same accounting for the scoring-only hints."""
        needles, _, _ = history.parse_search_query("11 11 22 33")

        hints = {n.text for n in needles if not n.required}
        assert hints == {"#11", "#22", "#33"}, hints
        assert sorted(n.text for n in needles if n.required) == ["11", "22", "33"]

    def test_bare_number_ranking_hints_share_the_expansion_budget(self):
        """Ranking hints cost scans too, so they draw on the same cap."""
        query = " ".join(str(100 + i) for i in range(history._SEARCH_MAX_FORGE_REFS + 3))

        needles, _, _ = history.parse_search_query(query)

        hints = [n for n in needles if not n.required]
        assert len(hints) == history._SEARCH_MAX_FORGE_REFS
        assert len([n for n in needles if n.required]) == history._SEARCH_MAX_FORGE_REFS + 3

    def test_count_needle_counts_every_spelling(self):
        """One counter for the alternation, so matcher and ranker cannot diverge."""
        needle = history.SearchNeedle("#7", 1.0, True, ("pull/7",), True)

        assert history.count_needle(needle, "#7 and /pull/7 and #7 again") == 3
        assert history.count_needle(needle, "#70 and /pull/70") == 0
        assert history.count_needle(needle, "") == 0

    def test_sigil_free_query_still_matches_its_own_words(self, tmp_path):
        """The never-hide invariant: a query typed without a sigil keeps the digits.

        "issue 42" gated on the digits before references existed, so a transcript
        saying exactly that must still match — the expansion may only loosen.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("prose", "assistant", "we hit issue 42 in prod and rolled back")
        log.append("sigil", "assistant", "filed #42 for the rollback")

        assert {s["key"] for s in log.search_sessions("issue 42", 10)} == {"prose", "sigil"}
        assert "prose" in {s["key"] for s in log.search_sessions("issue42", 10)}

    def test_a_sigil_query_stays_precise(self, tmp_path):
        """The other half: an explicit sigil never gated on bare digits, so it
        must not start matching every standalone number."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("count", "assistant", "12 files changed, 3 insertions")
        log.append("the_mr", "assistant", "see !12 for the fix")

        assert {s["key"] for s in log.search_sessions("!12", 10)} == {"the_mr"}

    def test_merge_number_is_not_a_reference_at_all(self, tmp_path):
        """\"merge 1234" is prose: "merge" names no type, so the words stay literal.

        Reading it as a reference would drop "merge" from the gate and pull in
        every session mentioning 1234. The ranking hint still surfaces the pull
        request first, which is what someone typing it wants.
        """
        needles, _, _ = history.parse_search_query("merge 1234")
        assert [n.text for n in needles if n.required] == ["merge", "1234"]

        log = ConversationLog(base_dir=tmp_path)
        log.append("noise", "assistant", "merge took 1234 ms, twice: 1234 ms again")
        log.append("gh_pr", "assistant", "merge #1234 after the rebase")

        keys = [s["key"] for s in log.search_sessions("merge 1234", 10)]

        assert keys[0] == "gh_pr", keys

    def test_chain_only_words_do_not_make_a_reference(self):
        """\"requests 12" is prose about requests, not item 12."""
        needles, _, _ = history.parse_search_query("requests 12")

        assert [n.text for n in needles if n.required] == ["requests", "12"]

    def test_a_repo_name_ending_in_a_digit_still_matches(self, tmp_path):
        """The left boundary guards the NUMBER, not the delimiter before it.

        A too-eager left boundary refuses a ``#``-number inside a repo name
        ending in a digit — the exact reference the query named.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("digit_repo", "assistant", "see kirocrew2#4411 for the fix")

        assert {s["key"] for s in log.search_sessions("kirocrew2#4411", 10)} == {"digit_repo"}
        assert {s["key"] for s in log.search_sessions("#4411", 10)} == {"digit_repo"}

    def test_digits_inside_a_longer_number_are_the_one_dropped_case(self, tmp_path):
        """The stated exception to the recall guarantee, pinned deliberately.

        A session whose only claim to the old substring match was the digits
        sitting inside a longer number never referenced the item, and excluding
        it is the whole purpose of the boundary — so this narrowing is intended,
        not a regression to fix. Both edges are covered: the run id below ENDS in
        the queried digits, which only the left guard rejects.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("trailing", "assistant", "pr build 99994411 timed out")
        log.append("leading", "assistant", "pr build 44110293 timed out")

        assert log.search_sessions("pr 4411", 10) == []

    @pytest.mark.parametrize(
        "query,still_required",
        [
            ("pr 4411", []),
            ("pull request 4411", []),
            ("issue 42", []),
            ("merge request 12", []),
            ("merge issue 42", ["merge"]),
            ("merge #12", ["merge"]),
            ("requests #12", ["requests"]),
            ("rebase pull request #4411", ["rebase"]),
        ],
        ids=[
            "one-type-word",
            "two-word-type",
            "issue",
            "gitlab-two-word",
            "term-then-type",
            "term-only",
            "chain-only",
            "term-outside-the-run",
        ],
    )
    def test_only_the_type_naming_suffix_leaves_the_gate(self, query, still_required):
        """Words before the type phrase are the user's own terms, not the reference.

        "merge issue 42" asks about `merge` AND issue 42; dropping `merge` would
        return every session mentioning #42. Only the suffix that names the type
        is discardable, and it is the SHORTEST naming suffix so a longer run
        cannot qualify on a type word buried inside it.
        """
        needles, _, _ = history.parse_search_query(query)

        required = sorted(n.text for n in needles if n.required and not n.alts)
        assert required == sorted(still_required), query

    def test_a_chain_only_word_before_a_sigil_stays_in_the_gate(self, tmp_path):
        """\"merge #12": the word is a search term the user typed, not a type name.

        Dropping it would return every session mentioning #12. This is the same
        rule the bare-digit branch applies — only a run that NAMES a type is
        discardable — and the sigil branch was skipping it.
        """
        needles, _, _ = history.parse_search_query("merge #12")
        assert sorted(n.text for n in needles if n.required) == ["#12", "merge"]

        log = ConversationLog(base_dir=tmp_path)
        log.append("unrelated", "assistant", "filed #12 against the parser")
        log.append("wanted", "assistant", "merge #12 once the gate is green")

        assert {s["key"] for s in log.search_sessions("merge #12", 10)} == {"wanted"}

    def test_a_type_word_before_a_sigil_is_still_dropped(self, tmp_path):
        """A leading-type-word query must still reach a transcript that only has the URL."""
        log = self._corpus(tmp_path)

        keys = {s["key"] for s in log.search_sessions("pull request #4411", 10)}

        assert "url_only" in keys, keys

    @pytest.mark.parametrize(
        "token",
        [
            "#4411",
            "!12",
            "pr#4411",
            "mr#12",
            "mr!12",
            "pr4411",
            "pr-4411",
            "pull/4411",
            "pulls/4411",
            "issues/42",
            "merge_requests/12",
            "kirodotdev/kirocrew#4411",
            "kirocrew2#4411",
            "https://github.com/kirodotdev/kirocrew/pull/4411",
            "https://gitlab.com/grp/proj/-/merge_requests/12",
        ],
    )
    def test_a_reference_always_matches_its_own_literal(self, tmp_path, token):
        """The structural recall guarantee: the query's own text is a spelling.

        The old gate required this exact string, so a shape whose derived
        spellings happen not to cover it must still match — otherwise a query
        fails against a transcript quoting it verbatim. `mr#12` was exactly that
        hole: its word said GitLab, its sigil said GitHub, and none of the
        spellings was the string typed.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("verbatim", "assistant", f"the reference is {token} in this line")

        assert {s["key"] for s in log.search_sessions(token, 10)} == {"verbatim"}, token

    def test_the_typed_sigil_decides_the_family(self):
        """A word before the sigil cannot override it: "#" is the shared sequence."""
        assert self._needle("mr#12").text == "#12"
        assert self._needle("pr!12").text == "!12"

    def test_a_path_form_matches_with_or_without_a_leading_slash(self, tmp_path):
        """Path spellings carry no leading slash, so both writings match.

        A transcript that writes ``pull/4411`` on its own was matched by the old
        literal gate; requiring ``/pull/4411`` would have dropped it.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("no_slash", "assistant", "pull/4411 is the one that fixes it")

        assert {s["key"] for s in log.search_sessions("pull/4411", 10)} == {"no_slash"}
        assert {s["key"] for s in log.search_sessions("#4411", 10)} == {"no_slash"}

    def test_merge_request_two_word_form_is_the_gitlab_family(self, tmp_path):
        """\"merge request 12" IS GitLab, and reaches the number through two words.

        It ranks the merge request first rather than excluding the issue: the
        query typed bare digits, so the digits stay a spelling (recall) and a
        standalone "12" still qualifies. Exclusivity belongs to the sigil forms,
        where the user was explicit — see the sigil tests. The family itself is
        asserted on the parsed needle, because the ranking hint a bare number
        carries would order these two rows the same way even if the reference
        were filed under the wrong family.
        """
        assert self._needle("merge request 12").text == "!12"

        log = ConversationLog(base_dir=tmp_path)
        log.append("gh_issue", "assistant", "filed #12 against the parser")
        log.append("gl_mr", "assistant", "see /merge_requests/12 for the fix")

        keys = [s["key"] for s in log.search_sessions("merge request 12", 10)]

        assert keys[0] == "gl_mr", keys

    def test_three_token_lead_chain_reaches_the_reference(self, tmp_path):
        """A two-word type lead: both words are dropped, not just the nearest."""
        log = self._corpus(tmp_path)

        keys = {s["key"] for s in log.search_sessions("pull request #4411", 10)}

        assert keys == {"url_only", "hash_form", "prose_form"}, keys


class TestProviderSearchRefSeam:
    """A REGISTERED source provider contributes its own id spellings.

    The forge shapes above are built in. An edition that adds a source provider
    whose ids look like ``REV-987654321`` matches none of them, so without a seam
    its ids degrade to plain literal needles: a query finds only the exact string
    it typed, never a transcript that cited the same item by URL. These pin the
    seam's two consultation points -- gating on a prefixed id, and RANKING for a
    bare number -- and the guards that keep a plugin from widening the gate or
    breaking the search box.

    The resolvers here are deliberately fake and generic: the seam is provider
    agnostic, and a test naming a real provider would encode one consumer's URL
    shapes into core's contract.
    """

    @staticmethod
    @pytest.fixture(autouse=True)
    def _clean_registry():
        """The resolver registry is module state, so isolate every test."""
        history_search.reset_search_ref_resolver_for_tests()
        yield
        history_search.reset_search_ref_resolver_for_tests()

    @staticmethod
    def _resolver(token: str):
        """Recognize ``ref-<digits>``, ``items/ref-<digits>`` and bare digits."""
        import re

        match = re.fullmatch(r"(?:ref-)?(\d{3,9})", token) or re.fullmatch(
            r"items/ref-(\d{3,9})", token
        )
        if match is None:
            return None
        number = match.group(1)
        return (f"ref-{number}", (f"items/ref-{number}", f"ref {number}"))

    @staticmethod
    def _needle(query: str) -> history.SearchNeedle:
        """The single required needle, asserting the one-reference invariant.

        Mirrors ``TestForgeReferenceSearch._needle``: ~30 forge tests depend on a
        reference query gating on exactly ONE needle, so a provider token must
        satisfy the same invariant or the literal term has survived into the gate
        alongside the provider's canonical spelling.
        """
        needles, _, _ = history.parse_search_query(query)
        required = [n for n in needles if n.required]
        assert len(required) == 1, f"{query!r} must gate on one reference: {required}"
        return required[0]

    @staticmethod
    def _corpus(tmp_path) -> ConversationLog:
        log = ConversationLog(base_dir=tmp_path)
        log.append("url_form", "assistant", "opened https://example.test/items/ref-987654321 today")
        log.append("id_form", "assistant", "babysitting ref-987654321 to green")
        log.append("prose_form", "assistant", "rebased ref 987654321 onto main")
        log.append("digit_noise", "assistant", "the run id was 1987654321772 and it timed out")
        return log

    def test_a_provider_id_gates_on_the_item_not_the_literal(self):
        """The named defect: one item, one result set, whichever spelling is typed."""
        history_search.register_search_ref_resolver(self._resolver)

        needle = self._needle("REF-987654321")

        assert needle.text == "ref-987654321"
        assert set(needle.alts) == {"items/ref-987654321", "ref 987654321"}
        assert needle.digit_bounded is True

    def test_a_provider_reference_with_a_short_spelling_is_saturated(self):
        """A provider reference saturates when any spelling is short."""
        history_search.register_search_ref_resolver(
            lambda token: ("x5", ("items/x5", "5")) if token.casefold() == "x5" else None
        )

        assert self._needle("X5").saturate_body is True

        history_search.register_search_ref_resolver(self._resolver)

        assert self._needle("REF-987654321").saturate_body is False

    def test_every_provider_spelling_finds_every_spelling(self, tmp_path):
        """A URL mention and a prose mention answer the same query."""
        history_search.register_search_ref_resolver(self._resolver)
        log = self._corpus(tmp_path)

        for query in ("ref-987654321", "items/ref-987654321"):
            keys = {s["key"] for s in log.search_sessions(query, 10)}
            assert keys == {"url_form", "id_form", "prose_form"}, query

    def test_an_uppercase_resolver_answer_still_matches(self, tmp_path):
        """Casefold on OUR side, because getting it wrong fails SILENTLY.

        A query is casefolded before parsing and needle matching requires folded
        text, so a spelling that arrived capitalized would produce a needle that
        can never match -- no error, no log, just zero results. Trusting the
        plugin to fold is therefore not an option.
        """

        def shouty(token: str):
            if token != "ref-987654321":
                return None
            return ("REF-987654321", ("ITEMS/REF-987654321", "REF 987654321"))

        history_search.register_search_ref_resolver(shouty)
        log = self._corpus(tmp_path)

        needle = self._needle("ref-987654321")

        assert needle.text == "ref-987654321"
        assert needle.alts == ("items/ref-987654321", "ref 987654321")
        assert {s["key"] for s in log.search_sessions("ref-987654321", 10)} == {
            "url_form",
            "id_form",
            "prose_form",
        }

    def test_a_bare_number_keeps_plain_substring_recall(self, tmp_path):
        """The loosen-only contract: a resolver cannot hide a numeric-content hit."""
        history_search.register_search_ref_resolver(self._resolver)
        log = self._corpus(tmp_path)

        keys = {s["key"] for s in log.search_sessions("987654321", 10)}

        assert "digit_noise" in keys, "plain digits still match"

    def test_built_ins_win_and_the_resolver_is_never_asked(self):
        """A resolver can only claim a token no built-in recognized."""
        asked: list[str] = []

        def greedy(token: str):
            asked.append(token)
            return ("ref-4411", ("items/ref-4411",))

        history_search.register_search_ref_resolver(greedy)

        for query in ("#4411", "pull/4411", "https://github.com/kirodotdev/KiroCrew/pull/4411"):
            needle = self._needle(query)
            assert needle.text == "#4411", query
            assert "pull/4411" in needle.alts, query
        assert asked == [], f"built-in shapes must short-circuit the resolver: {asked}"

    def test_a_resolver_cannot_gate_a_bare_number(self):
        """A bare number is the provider's blind spot: it is not consulted at all.

        A provider's ids are prefixed, so a bare run of digits names nothing it
        owns. Gating on it would trade a real search term for every session that
        merely mentions that number.
        """

        def greedy(token: str):
            return ("ref-42", ("items/ref-42",))

        history_search.register_search_ref_resolver(greedy)

        needles, _, _ = history.parse_search_query("42")

        required = [n for n in needles if n.required]
        assert [n.text for n in required] == ["42"], required
        # Nor does it reach the ranking hint: the hint carries built-in spellings
        # only, so a provider cannot influence a bare-number query in any way.
        folded = {s for n in needles if not n.required for s in (n.text, *n.alts)}
        assert not any("ref-42" in s for s in folded), folded

    def test_contributed_spellings_are_capped(self):
        """Each spelling costs one substring scan of every scanned session."""

        def flood(token: str):
            if token != "ref-777":
                return None
            return ("ref-777", tuple(f"spelling-{i}/777" for i in range(50)))

        history_search.register_search_ref_resolver(flood)

        needle = self._needle("ref-777")

        assert len(needle.alts) == history_search._MAX_SEARCH_REF_SPELLINGS
        # Bounds ONE answer, not a fan-in: the collector returns the first
        # plugin to recognize the token, so only one provider's answer arrives.
        assert history_search._MAX_SEARCH_REF_SPELLINGS == 8, "one answer's worth"

    def test_an_endless_alts_iterable_is_consumed_only_to_the_cap(self):
        """A resolver ignoring the ``Sequence`` contract can hand back no end of
        spellings, and materializing them all would hang every search."""
        produced: list[str] = []

        def endless(token: str):
            if token != "ref-777":
                return None

            def spellings():
                while True:
                    produced.append(f"spelling-{len(produced)}/777")
                    yield produced[-1]

            return ("ref-777", spellings())

        history_search.register_search_ref_resolver(endless)

        self._needle("ref-777")

        assert len(produced) == history_search._MAX_SEARCH_REF_SPELLINGS, len(produced)

    def test_an_answer_that_does_not_carry_the_typed_token_is_dropped(self):
        """Containment: an answer naming some OTHER item cannot claim the token."""

        def unrelated(token: str):
            return ("ref-111", ("items/ref-111",)) if token == "ref-777" else None

        history_search.register_search_ref_resolver(unrelated)

        needles, _, _ = history.parse_search_query("ref-777")

        required = [n for n in needles if n.required]
        assert [n.text for n in required] == ["ref-777"], required
        assert all(n.alts == () for n in required)

    def test_a_raising_resolver_does_not_break_the_query(self):
        """A plugin defect must not make the search box stop working."""

        def broken(token: str):
            raise RuntimeError("provider is on fire")

        history_search.register_search_ref_resolver(broken)

        needles, phrase, _ = history.parse_search_query("ref-987654321 deploy")

        assert phrase == "ref-987654321 deploy"
        assert {n.text for n in needles if n.required} == {"ref-987654321", "deploy"}

    def test_a_resolver_whose_unpack_raises_does_not_break_the_query(self):
        """The ANSWER itself may be lazy, so unpacking it can raise anything.

        Only ``TypeError``/``ValueError`` were caught, so a two-item generator that
        raises while being unpacked escaped ``parse_search_query`` into a 500 on
        every search -- the collector does not shape-check ahead of this.
        """

        def lazy_unpack_boom(token: str):
            def answer():
                yield "ref-987654321"
                raise RuntimeError("provider is on fire")

            return answer()

        history_search.register_search_ref_resolver(lazy_unpack_boom)

        needles, phrase, _ = history.parse_search_query("ref-987654321 deploy")

        assert phrase == "ref-987654321 deploy"
        assert {n.text for n in needles if n.required} == {"ref-987654321", "deploy"}

    def test_a_resolver_raising_while_its_alts_are_read_does_not_break_the_query(self):
        """The hook promises a ``Sequence``, which cannot raise while being read.

        A resolver ignoring that can, so the read sits inside a boundary; without
        one the exception escapes the parse as a 500 on every search. The answer
        is dropped WHOLE -- a half-read one is not an answer.
        """

        def boom_on_read(token: str):
            class Hostile:
                def __iter__(self):
                    yield "items/ref-987654321"
                    raise RuntimeError("provider is on fire")

            return ("ref-987654321", Hostile()) if token == "ref-987654321" else None

        history_search.register_search_ref_resolver(boom_on_read)

        needles, phrase, _ = history.parse_search_query("ref-987654321 deploy")

        assert phrase == "ref-987654321 deploy"
        assert {n.text for n in needles if n.required} == {"ref-987654321", "deploy"}
        gating = next(n for n in needles if n.required and n.text == "ref-987654321")
        assert gating.alts == (), gating.alts

    def test_a_later_registration_replaces_the_resolver(self):
        """One slot, not a list: the latest registration is the one consulted."""

        def broken(token: str):
            raise RuntimeError("provider is on fire")

        history_search.register_search_ref_resolver(broken)
        history_search.register_search_ref_resolver(self._resolver)

        needle = self._needle("ref-987654321")

        assert needle.text == "ref-987654321"

    def test_a_malformed_resolver_answer_is_ignored(self):
        """Shape is validated, not trusted: a bad answer degrades to a literal."""

        def malformed(token: str):
            return ("", ["ok/1"]) if token == "ref-987654321" else None

        history_search.register_search_ref_resolver(malformed)

        needles, _, _ = history.parse_search_query("ref-987654321")

        assert {n.text for n in needles if n.required} == {"ref-987654321"}
        assert all(n.alts == () for n in needles if n.required)

    def test_a_malformed_resolver_answer_is_logged_not_silent(self, caplog):
        """The docstring promises every failure leaves a trace; silence hides a defect.

        A silent drop is the failure this normalizer exists to prevent: the query
        degrades to a literal and returns zero results with nothing to read.
        """

        def bad_canonical(token: str):
            return (42, ["ok/1"]) if token == "ref-987654321" else None

        history_search.register_search_ref_resolver(bad_canonical)
        with caplog.at_level(logging.DEBUG, logger=history_search.logger.name):
            history.parse_search_query("ref-987654321")

        assert any("malformed" in r.message or "invalid" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_a_non_iterable_alts_answer_is_logged_not_silent(self, caplog):
        """The sibling shape rejection: `alts` a bare string is equally silent."""

        def bad_alts(token: str):
            return ("acme-987654321", "not-a-sequence") if token == "ref-987654321" else None

        history_search.register_search_ref_resolver(bad_alts)
        with caplog.at_level(logging.DEBUG, logger=history_search.logger.name):
            history.parse_search_query("ref-987654321")

        assert any("malformed" in r.message or "invalid" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]

    def test_a_malformed_alt_is_logged_not_silent(self, caplog):
        """The per-alt shape rejection, the third case the docstring's claim covers.

        One bad spelling costs only itself: the good spellings survive, so this is a
        per-alt drop rather than the whole-answer drop a raising read produces.
        """

        def bad_alt(token: str):
            if token != "ref-987654321":
                return None
            return ("acme-987654321", ["ref-987654321", 42, "  "])

        history_search.register_search_ref_resolver(bad_alt)
        with caplog.at_level(logging.DEBUG, logger=history_search.logger.name):
            needles, _, _ = history.parse_search_query("ref-987654321")

        assert any("an invalid alt" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]
        assert [n.alts for n in needles if n.required] == [("ref-987654321",)]

    def test_republishing_the_same_resolver_consults_it_once(self):
        """A registry republishes its collector on every provider registration."""
        calls: list[str] = []

        def counting(token: str):
            calls.append(token)
            return None

        history_search.register_search_ref_resolver(counting)
        history_search.register_search_ref_resolver(counting)

        history.parse_search_query("deploy")

        assert calls == ["deploy"], calls

    def test_no_registration_leaves_the_parse_untouched(self):
        """The seam is inert until something registers -- the regression guard."""
        needles, phrase, floor = history.parse_search_query("ref-987654321 4411")

        assert phrase == "ref-987654321 4411"
        assert {n.text for n in needles if n.required} == {"ref-987654321", "4411"}
        assert [n.text for n in needles if not n.required] == ["#4411"]
        assert floor is False

    def test_the_expansion_budget_is_shared_with_the_built_ins(self):
        """A provider token charges the same ledger, so it cannot mint slots."""
        history_search.register_search_ref_resolver(self._resolver)

        needles, _, _ = history.parse_search_query("ref-111 ref-222 ref-333 ref-444")

        required = [n for n in needles if n.required]
        expanded = [n for n in required if n.text.startswith("ref-") and n.alts]
        assert len(expanded) == history._SEARCH_MAX_FORGE_REFS, expanded
        assert required[-1].text == "ref-444", "the over-budget token degrades to a literal"
        assert required[-1].alts == ()

    def test_repeating_one_item_charges_one_slot(self):
        """Two spellings of one item are one item, whichever order they arrive."""
        history_search.register_search_ref_resolver(self._resolver)

        needles, _, _ = history.parse_search_query("ref-987654321 items/ref-987654321")

        required = [n for n in needles if n.required]
        assert len(required) == 1, required
        assert required[0].text == "ref-987654321"
