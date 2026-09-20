"""Coverage filter - the batch open-PR exclusion the queue build subtracts with.

The defect behind this script: a work source selects and excludes by LABEL, and a
contributor who opens a PR carrying ``Fixes #N`` applies no label, so an item
whose fix is already in flight is indistinguishable from a free one. Measured on
kirodotdev/KiroCrew, 25 of 29 label-clean candidates were referenced by an open
PR.

``claim_preflight.py`` already refuses those at claim time, so the risk in adding
a second answer to one question is DRIFT. Two things here are therefore about the
relationship rather than about this script alone:

* :class:`TestVocabularyAgreesWithClaimPreflight` pins the reference spellings
  against the other script's, and pins the one difference that is deliberate -- a
  bare mention is coverage here and is not closure there;
* every direction test asserts the filter can only SUBTRACT. ``UNCOVERED`` and
  ``UNKNOWN`` are the halves a careless edit would turn into permission, and an
  ``UNKNOWN`` that printed an ``uncovered`` list would read as a finding about
  items nothing scanned.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
)
SCRIPT = SKILL_DIR / "scripts" / "coverage_filter.py"
PREFLIGHT = SKILL_DIR / "scripts" / "claim_preflight.py"

REPO = "kirodotdev/KiroCrew"
ITEM = 11516


@pytest.fixture
def mod():
    return load_skill_script("coverage_filter", SCRIPT)


def pull(number: int, text: str, **overrides) -> dict:
    """One entry in the shape :func:`open_pull_requests` produces."""
    entry = {
        "number": number,
        "author": "someone",
        "is_cross_repository": False,
        "author_association": "MEMBER",
        "text": text,
    }
    entry.update(overrides)
    return entry


def api_pull(number: int, **overrides) -> dict:
    """One entry in the shape the forge's pulls endpoint returns."""
    entry = {
        "number": number,
        "title": f"fix: something ({number})",
        "body": "no references here",
        "user": {"login": "someone"},
        "author_association": "MEMBER",
        "head": {"repo": {"full_name": REPO}},
        "base": {"repo": {"full_name": REPO}},
    }
    entry.update(overrides)
    return entry


# --------------------------------------------------------------------------- #
# the reference vocabulary
# --------------------------------------------------------------------------- #


class TestItemReference:
    @pytest.mark.parametrize(
        "text",
        [
            "Fixes #11516",
            "closes #11516 and tidies up",
            "see #11516 for the measurement",  # a bare mention IS coverage here
            "Resolves kirodotdev/KiroCrew#11516",
            "refs https://github.com/kirodotdev/KiroCrew/issues/11516",
            "http://github.com/kirodotdev/KiroCrew/issues/11516",
            "FIXES KIRODOTDEV/KIROCREW#11516",
        ],
    )
    def test_every_spelling_the_forge_links_on_is_a_reference(self, mod, text):
        assert mod.item_reference_re(REPO, ITEM).search(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Fixes #115160",
            "Fixes #1151",
            "Fixes #21516",
            "the number 11516 with no hash",
            "Fixes otherowner/OtherRepo#11516",
            "no reference at all",
        ],
    )
    def test_a_neighbouring_number_is_not_a_reference(self, mod, text):
        """The digit boundary is load-bearing: without it a hash plus the item's
        digits also matches a LONGER number that merely starts with them, and the
        filter subtracts an item nobody is working on -- a silent denial of work,
        which is the one direction a subtractive filter can still get wrong."""
        assert mod.item_reference_re(REPO, ITEM).search(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "Fixes fakekirodotdev/KiroCrew#11516",
            "Fixes not-kirodotdev/KiroCrew#11516",
            "Fixes x.kirodotdev/KiroCrew#11516",
            "Fixes a/kirodotdev/KiroCrew#11516",
        ],
    )
    def test_an_owner_merely_ending_in_this_one_is_not_a_reference(self, mod, text):
        """The repository-qualified alternative carries the same left boundary as
        the bare form. Without it a foreign owner whose name ends in this one
        matches by substring, and the queue silently loses an item nobody is
        fixing - a false subtraction arriving through the second door rather than
        the first."""
        assert mod.item_reference_re(REPO, ITEM).search(text) is None

    def test_a_lookalike_owner_does_not_cover_the_item(self, mod):
        """End to end, not only on the pattern: a pull request whose body names a
        lookalike owner leaves the item UNCOVERED, so the queue keeps it."""
        pulls = [api_pull(41, body="Fixes fakekirodotdev/KiroCrew#11516")]
        found = mod.coverage(REPO, [ITEM], pulls)
        assert found[ITEM] == []

    def test_a_repo_name_with_a_dot_is_matched_literally(self, mod):
        """``re.escape`` on the repo, so ``a.b/c`` cannot match ``axb/c`` -- and
        the qualifier rule is what makes the negative half observable, since a
        bare ``#7`` would otherwise match inside either spelling."""
        pattern = mod.item_reference_re("a.b/c-d", 7)
        assert pattern.search("fixes a.b/c-d#7")
        assert pattern.search("fixes axb/c-d#7") is None


class TestVocabularyAgreesWithClaimPreflight:
    """Two scripts answer one question from different evidence. What must not
    drift is WHICH SPELLINGS name an item; what must differ is whether a closing
    keyword is required."""

    @pytest.fixture
    def preflight(self):
        return load_skill_script("claim_preflight", PREFLIGHT)

    @pytest.mark.parametrize(
        "text",
        [
            "Fixes #11516",
            "closes kirodotdev/KiroCrew#11516",
            "resolved https://github.com/kirodotdev/KiroCrew/issues/11516",
        ],
    )
    def test_anything_the_preflight_reads_as_closure_is_read_here_as_coverage(
        self, mod, preflight, text
    ):
        assert preflight.closing_reference_re(REPO, ITEM).search(text)
        assert mod.item_reference_re(REPO, ITEM).search(text)

    def test_a_bare_mention_is_coverage_here_and_not_closure_there(self, mod, preflight):
        """The deliberate difference, and the reason each side is right. CLOSE is
        the strongest answer the preflight has, so it demands a closing keyword. A
        queue subtraction is the weakest, and the preflight's own check 2 SKIPs on
        any open PR the timeline references -- keyword or not -- so requiring one
        here would admit items that the preflight then refuses, which is the
        rediscovery this script removes."""
        mention = "related to #11516, different fix"
        assert preflight.closing_reference_re(REPO, ITEM).search(mention) is None
        assert mod.item_reference_re(REPO, ITEM).search(mention)

    def test_both_scripts_reject_a_neighbouring_number(self, mod, preflight):
        assert preflight.closing_reference_re(REPO, ITEM).search("fixes #115160") is None
        assert mod.item_reference_re(REPO, ITEM).search("fixes #115160") is None

    @pytest.mark.parametrize(
        "text", ["fixes otherowner/OtherRepo#11516", "same shape as v1.2#11516"]
    )
    def test_a_qualified_number_is_declined_here(self, mod, text):
        """The one place this script is deliberately STRICTER, and the reason is
        the direction of its only failure mode: a subtractive filter that
        over-matches removes an item nobody is fixing, silently. A number
        carrying somebody else's repository, or a version, is not a reference to
        this repository's item.

        Only this side is asserted. The preflight's bare alternative reads the
        same text differently, and pinning that here would turn an improvement in
        another file into a red in this one - what needs guarding is THIS filter
        going lax."""
        assert mod.item_reference_re(REPO, ITEM).search(text) is None


# --------------------------------------------------------------------------- #
# the batch answer
# --------------------------------------------------------------------------- #


class TestCoverage:
    def test_a_body_reference_covers_the_item(self, mod):
        found = mod.coverage(REPO, [ITEM], [pull(12127, "Fixes #11516")])
        assert [hit["pr"] for hit in found[ITEM]] == [12127]

    def test_a_title_reference_covers_the_item(self, mod):
        found = mod.coverage(REPO, [ITEM], [pull(9, "fix(intake): #11516 coverage\n")])
        assert found[ITEM]

    def test_an_unreferenced_item_is_empty_rather_than_absent(self, mod):
        """Every item gets a key, so a caller cannot read an absent key as an
        unscanned item."""
        found = mod.coverage(REPO, [ITEM, 999], [pull(1, "Fixes #11516")])
        assert found[999] == []
        assert set(found) == {ITEM, 999}

    def test_a_fork_pr_is_coverage(self, mod):
        """Same rule as the preflight's check 2: in a public repository most
        genuine coverage arrives as a fork PR, so dropping those reinstates the
        duplicate-dispatch class."""
        found = mod.coverage(REPO, [ITEM], [pull(5, "Fixes #11516", is_cross_repository=True)])
        assert [hit["pr"] for hit in found[ITEM]] == [5]

    def test_a_draft_pr_is_coverage(self, mod):
        """A draft is work in flight, and the preflight counts it, so counting it
        here keeps the two readings identical rather than merely similar."""
        found = mod.coverage(REPO, [ITEM], [pull(6, "Fixes #11516", draft=True)])
        assert [hit["pr"] for hit in found[ITEM]] == [6]

    def test_several_prs_are_all_reported(self, mod):
        found = mod.coverage(REPO, [ITEM], [pull(1, "see #11516"), pull(2, "Fixes #11516")])
        assert [hit["pr"] for hit in found[ITEM]] == [1, 2]

    def test_an_empty_pr_text_is_skipped(self, mod):
        assert mod.coverage(REPO, [ITEM], [pull(1, "")])[ITEM] == []

    def test_an_unvouched_fork_is_annotated(self, mod):
        found = mod.coverage(
            REPO,
            [ITEM],
            [pull(7, "Fixes #11516", is_cross_repository=True, author_association="NONE")],
        )
        assert found[ITEM][0]["unvouched"] is True

    def test_an_insider_fork_is_not_annotated(self, mod):
        found = mod.coverage(
            REPO,
            [ITEM],
            [pull(7, "Fixes #11516", is_cross_repository=True, author_association="MEMBER")],
        )
        assert found[ITEM][0]["unvouched"] is False

    def test_a_same_repo_pr_is_never_annotated(self, mod):
        found = mod.coverage(REPO, [ITEM], [pull(7, "Fixes #11516", author_association="NONE")])
        assert found[ITEM][0]["unvouched"] is False
        assert found[ITEM][0]["fork"] is False


# --------------------------------------------------------------------------- #
# reading the forge
# --------------------------------------------------------------------------- #


class TestOpenPullRequests:
    def test_the_payload_becomes_the_shape_coverage_reads(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod, "gh_json", lambda args: ([api_pull(42, body="Fixes #11516")], None)
        )
        pulls, error = mod.open_pull_requests(REPO)
        assert error is None
        assert pulls[0]["number"] == 42
        assert pulls[0]["author"] == "someone"
        assert pulls[0]["is_cross_repository"] is False
        assert "#11516" in pulls[0]["text"]

    def test_a_cross_repository_head_is_a_fork(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod,
            "gh_json",
            lambda args: ([api_pull(42, head={"repo": {"full_name": "fork/KiroCrew"}})], None),
        )
        assert mod.open_pull_requests(REPO)[0][0]["is_cross_repository"] is True

    def test_a_deleted_head_repo_reads_as_a_fork(self, mod, monkeypatch):
        """The safe side: an unknown head is annotated rather than waved through
        as same-repo."""
        monkeypatch.setattr(mod, "gh_json", lambda args: ([api_pull(42, head={})], None))
        assert mod.open_pull_requests(REPO)[0][0]["is_cross_repository"] is True

    def test_only_open_pull_requests_are_requested(self, mod, monkeypatch):
        """A merged PR is the preflight's CLOSE rule, on ancestry, and a
        closed-unmerged one is abandoned work that FREES the item. Neither is a
        queue subtraction, so neither may be fetched here."""
        seen: list[list[str]] = []

        def record(args):
            seen.append(args)
            return [], None

        monkeypatch.setattr(mod, "gh_json", record)
        mod.open_pull_requests(REPO)
        assert "state=open" in seen[0][2]
        assert "--paginate" in seen[0]

    def test_an_entry_without_a_number_is_dropped(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "gh_json", lambda args: ([{"title": "x"}, "junk"], None))
        assert mod.open_pull_requests(REPO)[0] == []

    def test_a_failed_call_yields_a_slug_and_no_pulls(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "gh_json", lambda args: (None, "rate-limited"))
        assert mod.open_pull_requests(REPO) == ([], "rate-limited")

    def test_a_non_list_payload_is_a_failed_answer(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "gh_json", lambda args: ({"message": "nope"}, None))
        assert mod.open_pull_requests(REPO) == ([], "unparseable-json")


class TestNoWrites:
    @pytest.mark.parametrize(
        "args",
        [
            ["gh", "issue", "close", "1"],
            ["gh", "pr", "merge", "1"],
            ["git", "push"],
            ["gh"],
        ],
    )
    def test_a_mutating_or_unknown_argv_is_refused(self, mod, args):
        assert mod.is_read_only(args) is False

    @pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
    @pytest.mark.parametrize("spelling", ["-X", "--method", "--method="])
    def test_every_write_method_is_refused_in_every_spelling(self, mod, method, spelling):
        tail = [f"--method={method}"] if spelling == "--method=" else [spelling, method]
        assert mod.is_read_only(["gh", "api", "repos/o/r/pulls", *tail]) is False

    @pytest.mark.parametrize("flag", ["-f", "-F", "--field", "--raw-field", "--input"])
    def test_every_field_flag_is_refused(self, mod, flag):
        args = ["gh", "api", "repos/o/r/issues/1/comments", flag, "body=hi"]
        assert mod.is_read_only(args) is False

    def test_the_refused_vocabularies_hold_exactly_these_members(self, mod):
        """The two cases above spell every member out instead of reading the sets.

        A case list built from a set shrinks with it, so deleting a member would
        delete its own case and stay green. This equality is what turns a member
        added or removed later into a failing line someone has to answer for.
        """
        assert mod._WRITE_METHODS == {"POST", "PATCH", "PUT", "DELETE"}
        assert mod._FIELD_FLAGS == {"-f", "-F", "--field", "--raw-field", "--input"}

    @pytest.mark.parametrize(
        "args",
        [
            ["gh", "api", "repos/o/r/pulls?state=open", "--paginate"],
            ["gh", "api", "repos/o/r/pulls", "-X", "GET"],
            ["gh", "api", "repos/o/r/pulls", "--method=get"],
            ["gh", "pr", "list", "--state", "open"],
        ],
    )
    def test_a_read_argv_is_allowed(self, mod, args):
        assert mod.is_read_only(args) is True

    def test_run_gh_refuses_before_the_subprocess_exists(self, mod, monkeypatch):
        def explode(args):  # pragma: no cover - reaching it is the failure
            raise AssertionError("run_gh spawned a refused argv")

        monkeypatch.setattr(mod, "run", explode)
        rc, out, err = mod.run_gh(["gh", "pr", "merge", "1"])
        assert rc == 126
        assert out == ""
        assert "no writes" in err

    def test_a_refusal_reports_as_a_slug_not_as_stderr(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "run", lambda args: (0, "[]", ""))
        assert mod.gh_json(["gh", "pr", "merge", "1"]) == (None, "refused-write")


class TestErrorSlugs:
    @pytest.mark.parametrize(
        "rc,err,slug",
        [
            (127, "gh: No such file", "gh-missing"),
            (1, "API rate limit exceeded", "rate-limited"),
            (1, "HTTP 401: Bad credentials", "not-authenticated"),
            (1, "HTTP 404: Not Found", "not-found"),
            (1, "dial tcp: lookup api.github.com", "forge-unreachable"),
            (7, "something else entirely", "gh-error-rc7"),
        ],
    )
    def test_a_failure_becomes_a_slug(self, mod, rc, err, slug):
        assert mod.error_slug(rc, err) == slug

    def test_stderr_never_reaches_the_slug(self, mod):
        """Forge stderr can carry a URL with a token in it, and this value is
        printed into an agent's context."""
        secret = "https://api.github.com/x?access_token=abcd1234"
        assert secret not in mod.error_slug(9, secret)

    def test_unparseable_output_is_a_failed_answer(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "run", lambda args: (0, "{not json", ""))
        assert mod.gh_json(["gh", "api", "repos/o/r/pulls"]) == (None, "unparseable-json")

    def test_a_missing_binary_is_rc_127_not_a_traceback(self, mod):
        """The filter runs inside a conductor's cycle, so an absent ``gh`` has to
        arrive as a slug like any other unanswerable call."""
        rc, out, err = mod.run(["definitely-not-a-binary-11516"])
        assert rc == 127
        assert out == ""
        assert mod.error_slug(rc, err) == "gh-missing"

    def test_whitespace_only_output_parses_to_nothing(self, mod):
        assert mod.parse_pages("   \n  ") == []

    def test_concatenated_pages_are_merged(self, mod):
        assert mod.parse_pages('[{"number": 1}] [{"number": 2}]') == [
            {"number": 1},
            {"number": 2},
        ]

    def test_a_single_document_parses(self, mod):
        assert mod.parse_pages('{"number": 1}') == {"number": 1}

    def test_a_concatenated_object_page_is_kept(self, mod):
        assert mod.parse_pages('{"a": 1} {"b": 2}') == [{"a": 1}, {"b": 2}]


# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #


class TestParseItems:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("11516", [11516]),
            ("1,2,3", [1, 2, 3]),
            ("1 2\n3", [1, 2, 3]),
            (" 1 , 2 ", [1, 2]),
            ("2,1,2", [2, 1]),
        ],
    )
    def test_commas_and_whitespace_both_separate(self, mod, raw, expected):
        assert mod.parse_items(raw, "") == (expected, None)

    def test_stdin_supplies_the_list(self, mod):
        items, error = mod.parse_items("-", "10890\n10849\n")
        assert (items, error) == ([10890, 10849], None)

    @pytest.mark.parametrize("raw", ["", "   ", ","])
    def test_an_empty_list_is_malformed(self, mod, raw):
        items, error = mod.parse_items(raw, "")
        assert items == []
        assert "no items" in error

    @pytest.mark.parametrize("raw", ["abc", "12a", "-3", "0"])
    def test_a_non_number_is_malformed(self, mod, raw):
        items, error = mod.parse_items(raw, "")
        assert items == []
        assert "malformed item" in error

    def test_a_batch_beyond_the_cap_is_refused_rather_than_truncated(self, mod):
        """A truncated batch would report unscanned items as uncovered, which is
        the one shape this script must never print."""
        items, error = mod.parse_items(",".join(str(n) for n in range(1, mod.MAX_ITEMS + 2)), "")
        assert items == []
        assert "too many items" in error

    def test_a_batch_exactly_at_the_cap_is_accepted(self, mod):
        """The cap bounds what one forge call may carry, and a batch of exactly
        MAX_ITEMS is still that one call. Refusing it would reject a legal queue,
        so this case is what holds the comparison at `>` and not `>=`.
        """
        items, error = mod.parse_items(",".join(str(n) for n in range(1, mod.MAX_ITEMS + 1)), "")
        assert error is None
        assert len(items) == mod.MAX_ITEMS


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


class TestMain:
    @staticmethod
    def _with_pulls(mod, monkeypatch, pulls, error=None):
        monkeypatch.setattr(mod, "open_pull_requests", lambda repo: (pulls, error))

    def test_a_covered_item_is_reported_with_its_pr(self, mod, monkeypatch, capsys):
        self._with_pulls(mod, monkeypatch, [pull(12127, "Fixes #11516")])
        assert mod.main(["--repo", REPO, "--items", "11516,999"]) == 0
        out = capsys.readouterr().out
        assert "COVERED 11516 open-pr=#12127 fork=false author=someone" in out
        assert "UNCOVERED 999" in out
        assert "summary items=2 covered=1 uncovered=1 open-prs-read=1" in out

    def test_an_unvouched_fork_subtraction_is_loud(self, mod, monkeypatch, capsys):
        """The subtraction happens either way; what this buys is that the one
        suppression worth a look does not read identically to the routine ones."""
        self._with_pulls(
            mod,
            monkeypatch,
            [pull(8, "Fixes #11516", is_cross_repository=True, author_association="NONE")],
        )
        assert mod.main(["--repo", REPO, "--items", "11516"]) == 0
        assert "unvouched=true" in capsys.readouterr().out

    def test_extra_referencing_prs_are_named(self, mod, monkeypatch, capsys):
        self._with_pulls(mod, monkeypatch, [pull(1, "see #11516"), pull(2, "Fixes #11516")])
        assert mod.main(["--repo", REPO, "--items", "11516"]) == 0
        assert "also=#2" in capsys.readouterr().out

    def test_no_pull_request_prose_reaches_stdout(self, mod, monkeypatch, capsys):
        """A PR's title and body are read and never printed: this output lands in
        an agent's context, and identifiers are the whole evidence a conductor
        needs to check a subtraction."""
        self._with_pulls(
            mod, monkeypatch, [pull(3, "Fixes #11516 -- SECRETTITLE for the intake funnel")]
        )
        mod.main(["--repo", REPO, "--items", "11516"])
        assert "SECRETTITLE" not in capsys.readouterr().out

    def test_the_json_form_splits_covered_from_uncovered(self, mod, monkeypatch, capsys):
        self._with_pulls(mod, monkeypatch, [pull(12127, "Fixes #11516")])
        assert mod.main(["--repo", REPO, "--items", "11516,999", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["verdict"] == "OK"
        assert data["covered"]["11516"][0]["pr"] == 12127
        assert data["uncovered"] == [999]
        assert data["open_prs_read"] == 1

    def test_an_unreadable_forge_exits_three_and_names_no_uncovered_item(
        self, mod, monkeypatch, capsys
    ):
        """The direction that matters. An unanswered batch must not render as a
        finding about the items, so there is no ``uncovered`` key at all -- an
        empty one and a complete one would each read as one."""
        self._with_pulls(mod, monkeypatch, [], error="rate-limited")
        assert mod.main(["--repo", REPO, "--items", "11516,999", "--json"]) == 3
        data = json.loads(capsys.readouterr().out)
        assert data["verdict"] == "UNKNOWN"
        assert data["reason"] == "rate-limited"
        assert data["covered"] == {}
        assert "uncovered" not in data

    def test_the_human_unknown_line_carries_the_slug(self, mod, monkeypatch, capsys):
        self._with_pulls(mod, monkeypatch, [], error="forge-unreachable")
        assert mod.main(["--repo", REPO, "--items", "11516"]) == 3
        out = capsys.readouterr().out
        assert "UNKNOWN items=1 reason=forge-unreachable" in out
        assert "UNCOVERED" not in out

    @pytest.mark.parametrize("repo", ["nameonly", "owner/repo/extra", "owner /repo", ""])
    def test_a_malformed_repo_is_exit_two(self, mod, repo, capsys):
        assert mod.main(["--repo", repo, "--items", "1"]) == 2
        assert "malformed --repo" in capsys.readouterr().err

    def test_a_malformed_item_is_exit_two(self, mod, capsys):
        assert mod.main(["--repo", REPO, "--items", "abc"]) == 2
        assert "malformed item" in capsys.readouterr().err

    def test_a_bad_call_never_reads_the_forge(self, mod, monkeypatch):
        """Exit 2 is about the caller's arguments, so it must not spend a forge
        call to discover that -- and must not look like a finding either."""

        def explode(repo):  # pragma: no cover - reaching it is the failure
            raise AssertionError("a malformed call read the forge")

        monkeypatch.setattr(mod, "open_pull_requests", explode)
        assert mod.main(["--repo", "nope", "--items", "1"]) == 2

    def test_items_are_read_from_stdin(self, mod, monkeypatch, capsys):
        self._with_pulls(mod, monkeypatch, [pull(12127, "Fixes #11516")])
        monkeypatch.setattr("sys.stdin", io.StringIO("11516\n999\n"))
        assert mod.main(["--repo", REPO, "--items", "-"]) == 0
        out = capsys.readouterr().out
        assert "COVERED 11516" in out
        assert "UNCOVERED 999" in out
