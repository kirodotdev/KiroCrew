"""``github_comment_safety``: detect what would start the Kiro Agent app, rewrite nothing.

The Kiro Agent GitHub app starts a session, and often a competing pull request, on
an ISSUE comment whose raw markdown contains ``/kiro`` in any case. Core refuses
such a body and names the replacement to write instead; it never rewrites comment
markdown, and these tests pin both halves: every suggestion type is produced, and
the module exposes no function that returns a body at all.

They also pin the comments core itself tells a crew and the conductor to post: the
guard is the crew's only way to comment, so a template that cannot pass detection
is a crew that cannot report what it found.
"""

from __future__ import annotations

import re
from pathlib import Path
from time import perf_counter

import pytest

from kiro_crew import github_comment_safety as safety

_REPO_ROOT = Path(__file__).resolve().parents[1]
BRIEF = _REPO_ROOT / "src/kiro_crew/apps/builtins/issue_radar/backend/crew_brief.md"
CONDUCTOR = _REPO_ROOT / "src/kiro_crew/builtin_skills/pipeline-conductor/SKILL.md"
REPO = "kirodotdev/KiroCrew"  # brand-ok: the repository slug


# ── nothing here rewrites ────────────────────────────────────────────────────


def test_the_module_exposes_no_rewrite():
    # The property the redesign rests on: a caller cannot obtain a rewritten body
    # from this module even by accident, because no function returns one.
    assert set(safety.__all__) == {
        "MAX_REPORTED",
        "TRIGGER",
        "Match",
        "UnsafeCommentError",
        "assert_safe",
        "describe",
        "find_triggers",
    }
    assert not hasattr(safety, "neutralize")
    doc = " ".join((safety.__doc__ or "").split())
    assert "never rewrites comment markdown" in doc


# ── detection ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", ["/kiro", "/Kiro", "/KIRO", "/kIrO"])
def test_the_trigger_is_matched_in_any_case(spelling):
    (match,) = safety.find_triggers(f"see path{spelling}_crew/x.py here")
    assert match.token == f"path{spelling}_crew/x.py"


def test_a_clean_body_passes():
    body = "Fixed in #16047, see `kiro_crew/x.py line 10` and commit 641d6dd."
    assert safety.find_triggers(body, REPO) == []
    assert safety.assert_safe(body, REPO) is None


def test_a_body_with_the_word_but_no_slash_passes():
    assert safety.find_triggers("Kiro Crew and kirodotdev both appear here") == []


#: Every token shape the detector recognises, with the replacement it names.
SUGGESTION_CASES = [
    # An issue or pull-request URL in the target repository.
    ("https://github.com/kirodotdev/KiroCrew/pull/16047", "#16047"),
    ("https://github.com/kirodotdev/KiroCrew/issues/15139", "#15139"),
    ("https://github.com/kirodotdev/KiroCrew/pull/16047/files", "#16047"),
    ("https://github.com/KIRODOTDEV/kirocrew/issues/15139", "#15139"),
    # A commit, which is a short sha rather than the item it belongs to.
    ("https://github.com/kirodotdev/KiroCrew/commit/641d6dd1012345abcdef", "641d6dd"),
    ("https://github.com/kirodotdev/KiroCrew/pull/16047/commits/641d6dd1012345", "641d6dd"),
    # A blob or tree URL to a path, with the src/ prefix dropped.
    (
        "https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/x.py",
        "kiro_crew/x.py",
    ),
    (
        "https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/x.py#L10",
        "kiro_crew/x.py line 10",
    ),
    (
        "https://github.com/kirodotdev/KiroCrew/tree/main/src/kiro_crew/apps",
        "kiro_crew/apps",
    ),
    # An issue or pull request in ANOTHER repository: the whole ``owner/repo`` slug,
    # the one form GitHub links a cross-repository reference in.
    ("https://github.com/kiro-labs/widgets/issues/42", "kiro-labs/widgets#42"),
    ("https://github.com/kiro-labs/widgets/pull/7", "kiro-labs/widgets#7"),
    # A filesystem path as this repository spells it on disk.
    ("src/kiro_crew/cron/runner.py", "kiro_crew/cron/runner.py"),
    ("src/kiro_crew", "kiro_crew"),
]

#: Paths whose ``src/``-dropped form STILL holds the trigger, because a segment
#: inside them begins with ``kiro``. Dropping the prefix is no replacement here, so
#: the detector gives the advice instead of a suggestion it would refuse in turn.
NESTED_CASES = [
    "src/kiro_crew/agent_materialization/kiro_hooks.py",
    "src/kiro_crew/builtin_skills/kirocrew-dev/SKILL.md",
    "https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/agent_materialization"
    "/kiro_hooks.py",
    "https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/agent_materialization"
    "/kiro_hooks.py#L10",
    "https://github.com/kirodotdev/KiroCrew/tree/main/src/kiro_crew/builtin_skills/kirocrew-dev",
]

#: Issue and pull-request URLs in another repository whose own ``owner/repo`` slug
#: holds the trigger. The cross-repository form is the only one GitHub links, so
#: there is no replacement to offer here and the detector gives the advice.
CROSS_REPO_ADVICE_CASES = [
    "https://github.com/kirodotdev/Kiro/issues/42",
    "https://github.com/kirodotdev/kiro-docs/pull/7",
    "https://github.com/acme/kiro-docs/pull/7",
]

#: Tokens no shape fits, which get the advice on their own account.
OTHER_CASES = [
    "https://example.com/kiro/dashboard",
    "builtin_skills/kirocrew-dev",
    "website/src/pages/KiroCrewAgent/index.tsx",
    "/kiro",
]

#: What a crew is told when no replacement fits the token.
ADVICE = "write the path or URL without the slash before 'kiro'"


@pytest.mark.parametrize("token,suggestion", SUGGESTION_CASES)
def test_every_suggestion_type(token, suggestion):
    (match,) = safety.find_triggers(f"look at {token} for the fix", REPO)
    assert match.token == token
    assert match.suggestion == suggestion
    # A suggestion is useless if writing it would be refused in turn.
    assert safety.find_triggers(suggestion, REPO) == []


@pytest.mark.parametrize("token", NESTED_CASES)
def test_a_path_that_still_holds_the_trigger_gets_the_advice(token):
    # Dropping `src/` removes the trigger only when the prefix carries the only
    # slash before `kiro`. A nested segment keeps it, and a crew that wrote the
    # shortened path would be refused again, so the advice is what it gets.
    (match,) = safety.find_triggers(f"look at {token} for the fix", REPO)
    assert match.token == token
    assert match.suggestion == ADVICE
    assert safety.find_triggers(match.suggestion, REPO) == []


@pytest.mark.parametrize("token", CROSS_REPO_ADVICE_CASES)
def test_a_cross_repository_url_whose_slug_holds_the_trigger_gets_the_advice(token):
    # GitHub links a cross-repository reference only as `owner/repo#N`, and that
    # form keeps the trigger when the slug itself carries it. A crew that wrote it
    # would be refused again, so the advice is what it gets.
    (match,) = safety.find_triggers(f"look at {token} for the fix", REPO)
    assert match.token == token
    assert match.suggestion == ADVICE
    assert safety.find_triggers(match.suggestion, REPO) == []


def test_every_suggestion_the_detector_emits_passes_the_trigger_check():
    # The property the suggestions rest on: whatever a crew is told to write, that
    # text passes detection. One that did not would refuse the crew a second time
    # for following the instruction it was just given.
    tokens = (
        [token for token, _ in SUGGESTION_CASES]
        + NESTED_CASES
        + CROSS_REPO_ADVICE_CASES
        + OTHER_CASES
    )
    for token in tokens:
        for repo in (REPO, None):
            body = f"look at {token} for the fix"
            matches = safety.find_triggers(body, repo)
            assert matches, (token, repo)
            for match in matches:
                assert not safety.TRIGGER.search(match.suggestion), (token, repo, match.suggestion)
                assert safety.find_triggers(match.suggestion, repo) == []


@pytest.mark.parametrize("token", OTHER_CASES)
def test_anything_else_gets_advice_rather_than_a_replacement(token):
    (match,) = safety.find_triggers(f"at {token} today", REPO)
    assert match.token == token
    assert match.suggestion == ADVICE


def test_without_the_repo_a_url_into_this_repository_gets_the_advice():
    # ``check`` runs offline with no --repo, so every GitHub item URL is read as
    # cross-repository there. This repository's own slug holds the trigger, so the
    # `owner/repo#N` form is no replacement and the advice is what a crew sees.
    (match,) = safety.find_triggers("see https://github.com/kirodotdev/KiroCrew/pull/16047")
    assert match.suggestion == ADVICE
    assert safety.find_triggers(match.suggestion) == []


def test_one_match_per_url_even_when_it_holds_the_substring_twice():
    body = "https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/x.py#L10"
    (match,) = safety.find_triggers(body, REPO)
    assert match.token == body
    assert match.suggestion == "kiro_crew/x.py line 10"


def test_trailing_sentence_punctuation_is_not_part_of_the_token():
    (match,) = safety.find_triggers("fixed in src/kiro_crew/x.py.", REPO)
    assert match.token == "src/kiro_crew/x.py"


@pytest.mark.parametrize("wrapper", ["`{}`", "[label]({})", "<{}>", "({})", '"{}"'])
def test_markdown_around_a_token_is_not_swallowed_into_it(wrapper):
    (match,) = safety.find_triggers(wrapper.format("src/kiro_crew/x.py"), REPO)
    assert match.token == "src/kiro_crew/x.py"


def test_several_matches_carry_their_line_numbers_in_order():
    body = "\n".join(
        [
            "Root cause:",
            "- `src/kiro_crew/a.py`",
            "",
            "Fixed by https://github.com/kirodotdev/KiroCrew/pull/16047",
            "and src/kiro_crew/b.py",
        ]
    )
    matches = safety.find_triggers(body, REPO)
    assert [(m.line, m.suggestion) for m in matches] == [
        (2, "kiro_crew/a.py"),
        (4, "#16047"),
        (5, "kiro_crew/b.py"),
    ]
    # The spans are offsets into the body the caller passed in.
    assert [body[m.start : m.end] for m in matches] == [m.token for m in matches]


def test_a_crlf_body_keeps_its_line_numbers():
    body = "intro\r\nsee src/kiro_crew/x.py\r\n"
    (match,) = safety.find_triggers(body, REPO)
    assert match.line == 2


# ── the refusal ──────────────────────────────────────────────────────────────


def test_assert_safe_lists_each_place_with_its_replacement():
    body = "See https://github.com/kirodotdev/KiroCrew/pull/16047\nand src/kiro_crew/x.py"
    with pytest.raises(safety.UnsafeCommentError) as exc:
        safety.assert_safe(body, REPO)
    message = str(exc.value)
    assert "2 places hold the trigger" in message
    assert "line 1: https://github.com/kirodotdev/KiroCrew/pull/16047 -> #16047" in message
    assert "line 2: src/kiro_crew/x.py -> kiro_crew/x.py" in message
    assert "and 0 more" not in message


def test_one_place_is_reported_in_the_singular():
    with pytest.raises(safety.UnsafeCommentError) as exc:
        safety.assert_safe("src/kiro_crew/x.py", REPO)
    assert "1 place holds the trigger" in str(exc.value)


def test_the_message_is_capped_and_says_how_many_more():
    body = "\n".join(f"src/kiro_crew/f{n}.py" for n in range(12))
    with pytest.raises(safety.UnsafeCommentError) as exc:
        safety.assert_safe(body, REPO)
    message = str(exc.value)
    assert "12 places hold the trigger" in message
    assert message.count("  line ") == safety.MAX_REPORTED == 5
    assert f"... and {12 - 5} more" in message


def test_a_refusal_quotes_the_body_and_so_is_never_the_audit_reason():
    # The route audits a fixed reason instead (see pr_actions._pr_action_error).
    with pytest.raises(safety.UnsafeCommentError) as exc:
        safety.assert_safe("secret-looking src/kiro_crew/x.py", REPO)
    assert "src/kiro_crew/x.py" in str(exc.value)


# ── performance ──────────────────────────────────────────────────────────────


def test_a_comment_sized_body_is_checked_quickly():
    # GitHub caps a comment at 65536 characters, so this is the worst real input,
    # and nearly every line of it holds the trigger.
    line = "progress on src/kiro_crew/cron/runner.py and src/kiro_crew/x.py today\n"
    body = line * (65 * 1024 // len(line))
    started = perf_counter()
    matches = safety.find_triggers(body, REPO)
    elapsed = perf_counter() - started
    assert len(matches) > 1000
    assert elapsed < 0.25, f"{elapsed:.4f}s for {len(body)} characters"


def test_a_clean_comment_sized_body_is_checked_quickly():
    body = ("progress on `kiro_crew/cron/runner.py`, see #16047\n") * 1200
    started = perf_counter()
    assert safety.find_triggers(body, REPO) == []
    assert perf_counter() - started < 0.05


# ── core's own comments must pass detection ──────────────────────────────────


def _fenced_block(text: str, heading: str, index: int = 1) -> str:
    """The *index*-th fenced block after *heading*, as the instruction shows it."""
    section = text.split(heading, 1)[1]
    return section.split("```")[index]


#: Realistic values for the placeholders every template uses, written the way the
#: brief now tells a crew to write them: a reference as ``#N``, a commit as a short
#: sha, and a path in THIS repository without its ``src/`` prefix.
FILLINGS = {
    "<Your Name>": "Ada",
    "<Name>": "Ada",
    "<name>": "ada",
    "<phase>": "implementing",
    "<PR link if any>": "#2271",
    "<HH:MM>": "20:44",
    "<crew-id>": "c_7f3a",
    "<ISO8601 Z>": "2026-10-02T20:44:00Z",
    "<that timestamp>": "2026-10-02T18:31:00Z",
    "<n>": "15139",
}


def _fill(template: str) -> str:
    for placeholder, value in FILLINGS.items():
        template = template.replace(placeholder, value)
    return template


#: The comments core tells a crew to post, in the words the brief asks for. The two
#: literal templates are read out of the brief so a change there reaches this test;
#: the three mandatory comments and the release are prose instructions, so a
#: realistic body is written here for each.
def _crew_comments() -> dict[str, str]:
    brief = BRIEF.read_text(encoding="utf-8")
    claim = _fill(_fenced_block(brief, "### Claim comment format"))
    note = brief.split("Claim taken over by", 1)[1].split("```", 1)[0]
    takeover = _fill("Claim taken over by" + note)
    return {
        "claim": claim,
        "claim_progress": "\n".join(
            [
                "- `18:02` claimed — read the issue and `kiro_crew/cron/runner.py`",
                "- `18:14` confirmed not a duplicate — #2240 is a different code path",
                "- `18:31` branch `crew/ada/issue-15139` — fix plus a regression test",
                "- `19:58` opened PR #2271 at 641d6dd",
                "- `20:44` CI round 3 — 41/47 green, 6 reds inherited from main",
            ]
        ),
        "takeover": takeover,
        "found_conclusion": (
            "Root cause is in `kiro_crew/cron/runner.py line 120`: the wake deadline is\n"
            "recomputed from the turn end. Reproduced with `pytest test/test_cron.py -k wake`,\n"
            "which printed `assert 300 == 612`. Behaviour changed at 641d6dd. Not a duplicate\n"
            "of #2240 — that one is the scheduler, not the deadline."
        ),
        "found_pull_request": (
            "PR #2271 pins the deadline at arm time rather than at turn end, in\n"
            "`kiro_crew/cron/runner.py`. A user message defers a due fire without\n"
            "restarting the interval, which is a behaviour change beyond the bug; the\n"
            "skip-dates path is deliberately left alone."
        ),
        "found_needs_human": (
            "Two valid fixes with different behaviour, so this needs a decision.\n"
            "I read `kiro_crew/cron/runner.py` and `kiro_crew/cron/store.py`; either the\n"
            "deadline moves (shown in #2271) or the interval restarts. I recommend the\n"
            "first. Releasing the claim and moving on."
        ),
        "release": (
            "Published the question above and moved on — claim released, `crew: in progress`\n"
            "removed. Recorded as a skip with scope needs-decision."
        ),
    }


@pytest.mark.parametrize("name", sorted(_crew_comments()))
def test_every_crew_comment_passes_detection(name):
    body = _crew_comments()[name]
    assert "<" not in re.sub(r"</?(?:details|summary)>|<!--", "", body), body
    assert safety.find_triggers(body, REPO) == [], body


def test_the_conductor_stand_down_comment_passes_detection():
    # The skill's own posting instruction, plus the evidence comment it describes.
    body = (
        "Stood down: the item is covered by PR #16047, merged at 641d6dd.\n"
        "Claim and assignee released. Evidence: `kiro_crew/apps/builtins/issue_radar`\n"
        "carries the fix, and #15139 records the ruling."
    )
    assert safety.find_triggers(body, REPO) == []


@pytest.mark.parametrize("path", [BRIEF, CONDUCTOR], ids=lambda p: p.parent.name)
def test_the_instruction_states_that_core_never_rewrites(path):
    text = " ".join(path.read_text(encoding="utf-8").split())
    assert "The command never rewrites your body" in text
    assert "exits 3" in text


def test_the_takeover_section_rules_out_rewriting_the_dead_crew():
    raw = BRIEF.read_text(encoding="utf-8").split("### Taking over a dead claim", 1)[1]
    raw = raw.split("### Say what you found", 1)[0]
    section = " ".join(raw.split())
    # Copying the dead crew's words is out, and so is a second claim: either one
    # breaks a rule the protocol rests on.
    assert "rather than copying a word of it" in section
    assert "leave that comment untouched and post no second claim" in section
    assert "record the issue as a skip with that reason" in section
    # A refusal arrives after the stale label came off, so the branch has to undo
    # that and hand the issue to a person — otherwise the claim strands with its
    # non-terminal marker winning every later crew's tie-break.
    assert "Put the `crew:` label back as you found it" in section
    assert "apply the needs-human label named in the nudge" in section
    # The append-only rule stays absolute, with no exception carved into it.
    assert "never rewrite or delete a word of what is already there" in section
    assert "exception" not in section.lower()
    # The note refers to the claim rather than restating it.
    assert "The claim above was last updated" in section
