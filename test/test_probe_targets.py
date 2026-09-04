"""Target inference: what a monitor instruction says it is about.

The refusal cases carry the weight here. Failing to infer costs today's plain
timer; inferring the WRONG pull request costs a loop that is silent about its own
subject and chatty about a stranger. So the ambiguity tests are the point of the
file, not an afterthought.
"""

from __future__ import annotations

import json

from kiro_crew.probes import GH_PR
from kiro_crew.probes.targets import infer


def test_a_pr_url_is_inferred():
    target = infer("Babysit https://github.com/kirodotdev/KiroCrew/pull/7491 to green.")
    assert target is not None
    assert target.kind == GH_PR
    assert target.subject == "kirodotdev/KiroCrew#7491"
    assert json.loads(target.message) == {
        "repo": "kirodotdev/KiroCrew",
        "pr": 7491,
        # Pinned because the URL CARRIED the host: without it the probe addresses
        # a bare slug and an ambient enterprise GH_HOST resolves it elsewhere.
        "host": "github.com",
    }


def test_an_unconvertible_pr_number_is_refused_not_raised():
    """Inference runs on the ARMING path, so it must never raise.

    ``\\d+`` is unbounded and CPython refuses to convert a decimal string past its
    digit limit, so a pathological run of digits has to decline the match. Raising
    here would fail to arm the loop at all rather than merely decline to gate it.
    """
    huge = "9" * 5000
    target = infer(f"watch https://github.com/acme/widgets/pull/{huge} until green")
    assert target is None

    # A real subject beside the pathological one is still found.
    target = infer(
        f"ignore https://github.com/acme/widgets/pull/{huge}, watch "
        "https://github.com/kirodotdev/KiroCrew/pull/7491"
    )
    assert target is not None
    assert target.subject == "kirodotdev/KiroCrew#7491"


def test_a_chained_bare_reference_refuses_inference():
    """ "PRs #42 and #7" carries the prefix once and then relies on it.

    Matching only the first number let the second through unseen, so a loop gated on the
    URL for #42 would retire with the work on #7 unfinished -- the exact harm the whole
    ambiguity check exists to prevent, reached by the way a person naturally writes a
    pair.

    The chain is bounded to `#N` separated by a comma, `and` or `&`, so an unrelated
    number elsewhere in the instruction is not swept in and does not refuse a legitimate
    subject.
    """
    assert infer("watch PRs #42 and #7 https://github.com/acme/widgets/pull/42") is None
    assert infer("watch PRs #42, #7 https://github.com/acme/widgets/pull/42") is None
    assert infer("watch PRs #42 & #7 https://github.com/acme/widgets/pull/42") is None
    # The whole chain naming ONE subject is not ambiguous.
    same = infer("watch PR #42 and #42 at https://github.com/acme/widgets/pull/42")
    assert same is not None and same.subject == "acme/widgets#42"
    # A number outside the chain is not a pull-request reference at all.
    kept = infer("per the note in #7511, watch https://github.com/acme/widgets/pull/42")
    assert kept is not None and kept.subject == "acme/widgets#42"


def test_a_competing_bare_reference_refuses_inference():
    """`PR #42` is how a person actually names a pull request.

    "Babysit PR #42; blocked on <URL for #7>" gated on #7, so #7 merging retired a loop
    whose real work on #42 was unfinished -- the same harm the owner/repo shorthand
    check already guarded, reached through the commoner spelling. The bare form carries
    no owner or repo, so it can only signal ambiguity, never select a subject.

    Only a DIFFERENT number refuses. One subject named twice is the ordinary phrasing
    and must keep gating, or the feature would switch itself off on the way people
    normally write an instruction.
    """
    assert infer("Babysit PR #42; blocked on https://github.com/acme/widgets/pull/7") is None
    assert infer("watch pull request #9 then https://github.com/acme/widgets/pull/12") is None
    same = infer("watch PR #42 at https://github.com/acme/widgets/pull/42")
    assert same is not None and same.subject == "acme/widgets#42"
    # A source location is not a pull request reference, so it must not refuse.
    kept = infer("fix src/kiro_crew/autonudge.py#1751 for https://github.com/acme/widgets/pull/42")
    assert kept is not None and kept.subject == "acme/widgets#42"


def test_an_over_long_owner_or_repo_is_refused():
    """The quantifiers are bounded, and this is what that actually changes.

    CodeQL flagged ``[A-Za-z0-9._-]+`` followed by a literal as polynomial ReDoS
    (high). The bounds are GitHub's own limits -- 39 characters for an account, 100
    for a repository -- so nothing legitimate is lost, and a name longer than the
    real service allows is refused rather than partially matched.

    I first wrote this as a wall-clock timing assertion and it passed WITH THE BOUNDS
    REMOVED, so it proved nothing: the lookbehind and the URL anchor already restrict
    where a match may start, which is what keeps the practical cost linear. Keeping a
    test that cannot fail would have been worse than having none, so this asserts the
    consequence that does differ.
    """
    too_long_owner = "a" * 40
    assert infer(f"watch https://github.com/{too_long_owner}/widgets/pull/42") is None
    too_long_repo = "b" * 101
    assert infer(f"watch https://github.com/acme/{too_long_repo}/pull/42") is None
    # The longest names the real service allows still work.
    ok = infer(f"watch https://github.com/{'a' * 39}/{'b' * 100}/pull/42")
    assert ok is not None and ok.subject == f"{'a' * 39}/{'b' * 100}#42"


def test_a_competing_shorthand_refuses_the_url_rather_than_gating_on_it():
    """A shorthand cannot SELECT a subject, but it can show there is more than one.

    Round 23 deleted the shorthand pattern outright, which went too far. With only
    URLs scanned, the common shape "drive owner/name#42; blocked on <URL for #7>"
    gated on the BLOCKER -- so #7 merging retired a loop whose own work was #42. The
    URL is only authoritative when nothing else in the text names a different pull
    request.
    """
    assert (
        infer(
            "Drive kirodotdev/KiroCrew#42 to green; it is blocked on "
            "https://github.com/kirodotdev/KiroCrew/pull/7 merging first."
        )
        is None
    )
    # A line reference must NOT manufacture that ambiguity -- the lookbehind is what
    # keeps a cited source location out of it.
    target = infer(
        "Babysit https://github.com/kirodotdev/KiroCrew/pull/7491; the guard lives "
        "at src/kiro_crew/autonudge.py#1751."
    )
    assert target is not None and target.subject == "kirodotdev/KiroCrew#7491"


def test_a_shorthand_subject_is_refused_so_the_loop_stays_ungated():
    """``owner/name#123`` proves neither that it is a PULL REQUEST nor which SERVER.

    ``#123`` is equally an issue reference, and a same-numbered pull request may
    exist and be merged -- which would retire a loop that was watching the issue.
    The slug also resolves through the operator's ambient gh configuration, so on
    an enterprise host it names a different repository altogether; that ambiguity
    produced three separate review findings before this narrowing.

    Two earlier tests here asserted the opposite -- that a shorthand IS inferred,
    and that it is deliberately not host-pinned. Both were coherent while the
    shorthand was accepted; requiring the full URL removes the case they described
    rather than changing the answer to it. A shorthand-only instruction is simply
    not gated, which costs a turn per interval: today's cost, and the safe
    direction.
    """
    assert infer("watch kirodotdev/KiroCrew#7491 until it is green") is None
    assert infer("BABYSIT kirodotdev/KiroCrew#7435 (fix/knowledge-sync) to green.") is None


def test_the_url_and_shorthand_for_one_pr_are_one_subject():
    """Both spellings of the SAME pull request must not read as ambiguity."""
    target = infer(
        "Drive kirodotdev/KiroCrew#7491 -- "
        "https://github.com/kirodotdev/KiroCrew/pull/7491 -- to review-ready."
    )
    assert target is not None
    assert target.subject == "kirodotdev/KiroCrew#7491"


def test_two_distinct_pull_requests_refuse_rather_than_pick_one():
    """The real shape this protects against: a PR plus the PR it waits on.

    Picking the first mention would arm the watch on the blocker, which then
    reports the blocker's CI while the loop's own PR goes unwatched.
    """
    assert (
        infer(
            "PR kirodotdev/KiroCrew#4327 is gated on kirodotdev/KiroCrew#4137 "
            "merging first. Wait for it."
        )
        is None
    )


def test_two_repositories_refuse():
    assert infer("watch acme/widgets#1 and acme/gadgets#1") is None


def test_text_with_no_pull_request_returns_none():
    assert infer("Keep checking the deployment until the canary is healthy.") is None


def test_a_bare_issue_number_is_not_a_target():
    """``#7527`` alone names no repository, so it cannot be observed."""
    assert infer("gh-autofix issue #7527, keep an eye on it") is None


def test_an_enterprise_host_is_not_treated_as_github_com():
    assert infer("https://github.example.com/acme/widgets/pull/9") is None


def test_pr_zero_is_refused():
    assert infer("https://github.com/acme/widgets/pull/0") is None


def test_a_source_path_with_a_line_ref_is_not_a_target():
    """``src/kiro_crew/autonudge.py#1751`` names a code location, not a PR.

    Babysit instructions cite source locations constantly, and this shape reads
    as owner/repo#number to a naive pattern. Inferring it would arm the watch on
    a repository that does not exist, so the loop would go quiet about its own
    subject and every tick would burn a failed gh call.
    """
    assert infer("fix the guard at src/kiro_crew/autonudge.py#1751") is None
    assert infer("see website/src/pages/ChatPage.tsx#698") is None


def test_a_real_target_still_infers_when_a_path_ref_is_nearby():
    target = infer(
        "Babysit https://github.com/kirodotdev/KiroCrew/pull/7491; the guard lives "
        "at src/kiro_crew/autonudge.py#1751."
    )
    assert target is not None
    assert target.subject == "kirodotdev/KiroCrew#7491"


def test_empty_and_non_string_input_return_none():
    assert infer("") is None
    assert infer(None) is None  # type: ignore[arg-type]


def test_a_real_babysit_instruction_infers_its_own_pr():
    """Verbatim shape of a live loop's armed message (trimmed)."""
    text = (
        "BABYSIT PR #7542 (kirodotdev/KiroCrew, branch refactor/delete-dead-fence-"
        "helpers, worktree /home/u/oss/kirocrew-fix-4919, Closes #4919, follow-up "
        "#7540).\n\nEXIT: when 0 red checks, PR Readiness passed and MERGEABLE -- "
        "post a final message whose first line is `GREEN: PR #7542 "
        "https://github.com/kirodotdev/KiroCrew/pull/7542`"
    )
    target = infer(text)
    assert target is not None
    assert target.subject == "kirodotdev/KiroCrew#7542"
