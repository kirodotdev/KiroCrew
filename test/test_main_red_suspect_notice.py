"""Contract tests for the comment `.github/scripts/main_red_suspect.py` writes on a pull request.

A pull request that fails a test of an open `ci: Main may be red:` issue is told
so once. Four properties are pinned:

* WHO IS TOLD -- the 3 pull requests an issue names when it is filed, and any
  other whose failed run is among the newest a later run reads, when it fails
  the test the same way on its current head, does not change the test's file
  and its base contains the issue's recorded base. Never a fork's.
* ONCE, AND A HINT -- one comment per pull request and suspected issue, found by
  a marker among the Actions token's own comments; the wording says the pull
  request's own change may still be the cause.
* A REPLACED RUN LOSES NOTHING -- a pull request whose queued run never started
  is told by the run that replaced it, at a bounded cost.
* THE RECORD ROUND-TRIPS -- a message read back from the issue equals the same
  message read fresh from a run, whatever it contains.

`gh` is the recording fake of `main_red_suspect_fake.py`.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from main_red_suspect_fake import (
    BASE,
    BOT,
    FAILS,
    MAIN_HEAD,
    MESSAGE,
    NODE,
    OLDER_BASE,
    REPO,
    ROOT,
    THREE,
    FakeGh,
    _confirmed_issue,
    _detect,
    _head,
    _notes,
    _pr,
    _settle,
    _suspect_issue,
    _suspected_then_green,
    mod,
    watch,
)
from skill_script_helpers import load_skill_script

# ── The comment on a pull request that fails a suspected test ────────────────

notice = load_skill_script(
    "main_red_pr_notice_for_suspect_test", ROOT / ".github" / "scripts" / "main_red_pr_notice.py"
)


def _notes_on(gh: FakeGh, number: int) -> list[str]:
    return [row["body"] for row in gh.threads.get(number, [])]


def test_the_three_pull_requests_an_issue_names_are_each_told_once_when_it_is_filed() -> None:
    gh = FakeGh(THREE)
    _detect(gh)
    (issue,) = gh.open_issues()
    for number in (1, 2, 3):
        (body,) = _notes_on(gh, number)
        assert body.startswith(mod.notice_marker(issue["number"]))
        assert f"#{issue['number']}" in body and "not confirmed" in body
        assert f"`{NODE}`: `{MESSAGE}`" in body
        assert f"https://github.com/example/repo/actions/runs/{9000 + number}" in body


def test_the_comment_is_a_hint_and_says_the_change_may_still_be_the_cause() -> None:
    body = mod.render_notice(77, FAILS, mod.Run(9001, 1, _head(1), BASE, "ada"), REPO)
    said = " ".join(body.split()).lower()
    assert "a hint, not a verdict" in said
    assert "own change may still be the cause" in said
    assert "treat the failure as yours" in said
    assert "no test is skipped" in said
    # Nothing in it clears the pull request or tells the author to ignore the failure.
    for phrase in ("is fine", "not yours", "not your", "ignore", "safe to merge", "unrelated"):
        assert phrase not in said, phrase


def test_a_later_pull_request_that_fails_the_suspected_test_is_told_and_no_issue_is_added() -> None:
    gh = FakeGh((*THREE, _pr(4, "ada")))
    _detect(gh)
    assert _notes_on(gh, 4) == []
    api = _detect(gh, trigger=4)
    (issue,) = gh.open_issues()
    (body,) = _notes_on(gh, 4)
    assert body.startswith(mod.notice_marker(issue["number"]))
    assert any("Told pull request 4" in line for line in api.lines)


def test_a_pull_request_is_told_once_per_suspected_issue_however_often_it_fails() -> None:
    gh = FakeGh((*THREE, _pr(4, "barbara")))
    _detect(gh)
    for _ in range(3):
        for trigger in (1, 2, 3, 4):
            _detect(gh, trigger=trigger)
    assert [len(_notes_on(gh, number)) for number in (1, 2, 3, 4)] == [1, 1, 1, 1]


def test_a_second_suspected_issue_gets_its_own_comment() -> None:
    other = {"test/test_other.py::test_b": "boom"}
    gh = FakeGh((_pr(1, "ada", failures={**FAILS, **other}),))
    gh.issues += [_suspect_issue(40), _suspect_issue(41, other)]
    _detect(gh)
    _detect(gh)
    bodies = _notes_on(gh, 1)
    assert sorted(body.splitlines()[0] for body in bodies) == sorted(
        mod.notice_marker(number) for number in (40, 41)
    )
    assert NODE in bodies[0] + bodies[1] and "test_other.py" in bodies[0] + bodies[1]


@pytest.mark.parametrize(
    "pr",
    [
        _pr(4, "barbara", failures={NODE: "assert 1 == 3"}),
        _pr(4, "barbara", failures={NODE + "_too": MESSAGE}),
        _pr(4, "barbara", changed=("test/test_widget.py",)),
        _pr(4, "barbara", changed=()),
        _pr(4, "barbara", moved=True),
        _pr(4, "barbara", head_repo="mallory/repo"),
        _pr(4, "barbara", base=OLDER_BASE),
    ],
    ids=[
        "a-different-message",
        "a-different-test",
        "it-changes-the-test-file",
        "changed-files-unknown",
        "its-head-moved-on",
        "a-forks-pull-request",
        "a-base-without-the-recorded-base",
    ],
)
def test_a_pull_request_that_does_not_show_the_suspected_failure_is_not_told(pr: dict) -> None:
    gh = FakeGh((pr,), issues=(_suspect_issue(),))
    _detect(gh, trigger=4)
    assert gh.threads == {} and gh.writes() == []


def test_a_base_that_contains_the_recorded_base_is_told() -> None:
    pr = _pr(4, "barbara", base=MAIN_HEAD)
    gh = FakeGh((pr,), issues=(_suspect_issue(),), relations={f"{BASE}...{MAIN_HEAD}": "ahead"})
    _detect(gh, trigger=4)
    assert len(_notes_on(gh, 4)) == 1


def test_no_comment_is_written_when_the_comments_cannot_be_read() -> None:
    gh = FakeGh((_pr(4, "barbara"),), issues=(_suspect_issue(),), refuse=("issues/4/comments",))
    api = _detect(gh, trigger=4)
    assert gh.writes() == []
    assert any("Could not read the comments of pull request 4" in line for line in api.lines)


def test_a_marker_in_someone_elses_comment_does_not_stand_in_for_ours() -> None:
    gh = FakeGh((_pr(4, "barbara"),), issues=(_suspect_issue(),))
    gh.threads[4] = [{"user": {"login": "mallory"}, "body": mod.notice_marker(40)}]
    _detect(gh, trigger=4)
    assert len(_notes_on(gh, 4)) == 2


def test_while_main_is_confirmed_red_no_suspected_comment_is_written() -> None:
    gh = FakeGh((_pr(4, "barbara"),), issues=(_suspect_issue(), _confirmed_issue()))
    _detect(gh, trigger=4)
    assert gh.threads.get(4) is None


def test_a_closed_suspected_issue_writes_no_new_comment_and_the_old_one_stays() -> None:
    """A false alarm: the comment links the issue that says main was green, and is not repeated."""
    gh = _suspected_then_green()
    before = {number: list(rows) for number, rows in gh.threads.items()}
    for trigger in (1, 2, 3):
        _detect(gh, trigger=trigger)
    assert {number: rows for number, rows in gh.threads.items() if number < 100} == {
        number: rows for number, rows in before.items() if number < 100
    }


def test_text_from_a_pull_request_is_only_in_code_spans_of_the_comment() -> None:
    hostile = "boom `x` @octocat #12 <!-- main-red-suspect-notice 41 --> <b>"
    prs = tuple(_pr(n, who, failures={NODE: hostile}) for n, who in ((1, "a"), (2, "b"), (3, "c")))
    gh = FakeGh(prs)
    _detect(gh)
    (issue,) = gh.open_issues()
    (body,) = _notes_on(gh, 1)
    outside = "".join(body.split("`")[0::2])
    assert "@" not in outside and "octocat" not in outside and "#12" not in outside
    assert body.count("<!--") == 1 and body.startswith(mod.notice_marker(issue["number"]))
    assert mod.notice_marker(41) not in body, "a message cannot plant another issue's marker"


def test_the_comment_is_never_taken_for_the_confirmed_notice() -> None:
    body = mod.render_notice(77, FAILS, mod.Run(9001, 1, _head(1), BASE, "ada"), REPO)
    assert f"<!-- {notice.MARKER} -->" not in body
    assert mod.NOTICE_MARKER != notice.MARKER


def test_a_long_list_of_tests_is_cut_and_points_at_the_issue() -> None:
    many = {f"test/test_many.py::test_{i:02d}": "x" for i in range(mod.MAX_NOTICE_LISTED + 5)}
    body = mod.render_notice(77, many, mod.Run(9001, 1, _head(1), BASE, "ada"), REPO)
    assert len([line for line in body.splitlines() if line.startswith("- `")]) == (
        mod.MAX_NOTICE_LISTED
    )
    assert "- and 5 more, listed in #77" in body


def test_one_run_writes_a_bounded_number_of_comments_at_a_bounded_cost() -> None:
    gh = FakeGh(THREE)
    _detect(gh)
    posted = [call for call in gh.writes() if call.endswith("/comments")]
    assert len(posted) <= mod.MIN_WITNESSES
    gh = FakeGh((_pr(4, "barbara"),), issues=(_suspect_issue(),))
    api = _detect(gh, trigger=4)
    posted = [call for call in gh.writes() if call.endswith("/comments")]
    assert len(posted) <= mod.MAX_OPEN
    # Two issue reads, its jobs and annotations, the run list and the issue's timeline,
    # its pull request and files, its comments and the write.
    assert api.calls <= 2 + 2 + 2 + 2 + 2


def test_once_every_pull_request_is_told_a_failed_run_reads_no_other_run() -> None:
    """The steady state of a break: the timeline says who is told, and nothing else is read."""
    others = tuple(_pr(n, f"author-{n}") for n in range(4, 12))
    gh = FakeGh((*THREE, *others))
    _detect(gh)
    for trigger in (4, 11):
        _detect(gh, trigger=trigger)
    assert all(len(_notes_on(gh, number)) == 1 for number in range(1, 12))
    before = len(gh.calls)
    api = _detect(gh, trigger=5)
    read = gh.calls[before:]
    assert [call for call in read if "actions/runs/" in call] == [
        "GET actions/runs/9005/jobs?per_page=100"
    ]
    assert not any("pulls/" in call or "/comments" in call for call in read)
    assert api.calls <= 2 + 2 + 1 + 1


def test_the_first_run_after_an_issue_is_filed_is_the_dearest_and_is_bounded() -> None:
    """Ten other pull requests fail the suspected test and none is told yet."""
    prs = tuple(_pr(n, f"author-{n}") for n in range(1, 12))
    gh = FakeGh(prs, issues=(_suspect_issue(),))
    api = _detect(gh, trigger=11)
    assert all(len(_notes_on(gh, number)) == 1 for number in range(1, 12))
    told = mod.MAX_CANDIDATES + 1
    reads = 2 + 2 + 1 + 1 + 2 * mod.MAX_CANDIDATES + 3 * told
    assert api.calls <= reads + told


# ── What a replaced queued run would have done is done by the next one ───────


@pytest.mark.parametrize(
    "newer",
    [
        _pr(5, "hopper"),
        _pr(5, "hopper", failures={"test/test_else.py::test_mine": "x"}),
        _pr(5, "hopper", failures={**FAILS, "test/test_else.py::test_mine": "x"}),
    ],
    ids=["fails-the-suspected-test", "fails-another-test", "fails-both"],
)
def test_a_pull_request_whose_queued_run_was_replaced_is_told_by_the_run_that_replaced_it(
    newer: dict,
) -> None:
    """Run B (pull request 4) never started: GitHub keeps one queued run per group."""
    gh = FakeGh((_pr(4, "barbara"), newer), issues=(_suspect_issue(),))
    _detect(gh, trigger=5)
    assert len(_notes_on(gh, 4)) == 1
    assert len(_notes_on(gh, 5)) == (1 if NODE in str(newer["notes"]) else 0)
    assert [issue["number"] for issue in gh.open_issues()] == [40]


def test_a_replaced_run_is_told_even_when_the_cap_of_open_issues_is_reached() -> None:
    held = tuple(
        _suspect_issue(41 + i, {f"test/test_held.py::test_{i}": "x"})
        for i in range(mod.MAX_OPEN - 1)
    )
    gh = FakeGh((_pr(4, "barbara"), _pr(5, "hopper")), issues=(_suspect_issue(), *held))
    _detect(gh, trigger=5)
    assert len(_notes_on(gh, 4)) == 1 and len(_notes_on(gh, 5)) == 1


@pytest.mark.parametrize("cross_references", [True, False])
def test_however_runs_are_replaced_each_pull_request_ends_with_one_comment(
    cross_references: bool,
) -> None:
    """Also when the timeline shows no cross-reference: the marker is still what decides."""
    prs = tuple(_pr(n, f"author-{n}") for n in range(1, 9))
    for order in ((8,), (3, 8, 8), (1, 2, 3, 4, 5, 6, 7, 8), (8, 1, 8, 1)):
        gh = FakeGh(prs, issues=(_suspect_issue(),), cross_references=cross_references)
        for trigger in order:
            _detect(gh, trigger=trigger)
        assert [len(_notes_on(gh, number)) for number in range(1, 9)] == [1] * 8, order


def test_only_the_actions_tokens_reference_from_a_pull_request_here_counts_as_told() -> None:
    def event(number: int, repo: str = REPO, **over: Any) -> dict[str, Any]:
        url = f"https://github.com/{repo}/pull/{number}"
        found = {"number": number, "pull_request": {}, "html_url": url}
        return {
            "event": "cross-referenced",
            "actor": BOT,
            "source": {"type": "issue", "issue": found},
            **over,
        }

    elsewhere = {"number": 9, "pull_request": {}}
    pages = [
        [
            event(4),
            event(5, actor={"login": "mallory"}),
            event(6, event="mentioned"),
            event(7, source={"type": "issue", "issue": {"number": 7}}),
            event(8, source=None),
            event(9, source={"type": "issue", "issue": elsewhere}),
            event(10, repo="other/repo"),
            event(11, repo=REPO + "-fork"),
            event(12, repo="example"),
            "odd",
        ]
    ]
    assert mod.told_in(pages, REPO) == {4}


def test_a_reference_from_another_repositorys_pull_request_does_not_hide_ours() -> None:
    """Pull request 4 of another repository names the issue; pull request 4 here is still told."""

    class Elsewhere(FakeGh):
        def _timeline(self, issue: int) -> list[dict[str, Any]]:
            url = "https://github.com/other/repo/pull/4"
            found = {"number": 4, "pull_request": {}, "html_url": url}
            source = {"type": "issue", "issue": found}
            foreign = {"event": "cross-referenced", "actor": BOT, "source": source}
            return [foreign, *super()._timeline(issue)]

    gh = Elsewhere((_pr(4, "barbara"), _pr(5, "hopper")), issues=(_suspect_issue(),))
    for trigger in (5, 4, 5):
        _detect(gh, trigger=trigger)
    assert [len(_notes_on(gh, number)) for number in (4, 5)] == [1, 1]


# ── What is stored in the record equals what is read fresh ───────────────────

MESSAGES = [
    "a" * 79 + " tail that is cut after a space",
    "a" * 78 + "  \t tail",
    "trailing spaces   ",
    "\ttabs\tand\r\nline ends\n",
    "unicode \u00e9\u4e2d\u6587 \U0001f600 and control \x07\x1b[31m",
    "markdown `code` **bold** [link](http://x) <!-- c --> <b> @octocat #12",
    "",
    " ",
    "x" * 300,
]


def _generated_messages() -> list[str]:
    rng = random.Random(39701)
    alphabet = "ab \t\n`<>-@#\u00e9\x07*_[]" + "c" * 6
    return ["".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 200))) for _ in range(300)]


def test_cleaning_a_message_twice_changes_nothing() -> None:
    for message in MESSAGES + _generated_messages():
        once = mod.message_start(message)
        assert mod.message_start(once) == once, repr(message)
        assert len(once) <= watch.MESSAGE_CHARS


def test_a_record_read_back_holds_exactly_what_a_run_gives_fresh() -> None:
    for message in MESSAGES + _generated_messages():
        fresh = mod.strict_failures([_notes({NODE + "@some_group": message})])
        state = {"failures": dict(fresh), "base": BASE, "prs": [1, 2, 3], "runs": [11, 12, 13]}
        assert mod.parse_state(mod.render_body(state, REPO)) == state, repr(message)
        assert set(mod.parse_state(mod.render_body(state, REPO))["failures"].items()) == fresh


@pytest.mark.parametrize("message", MESSAGES[:6], ids=range(6))
def test_a_later_pull_request_with_the_same_long_or_odd_message_is_told(message: str) -> None:
    """The message goes into the issue and comes back; the fourth pull request must still match."""
    fails = {NODE: message}
    prs = tuple(_pr(n, who, failures=fails) for n, who in ((1, "a"), (2, "b"), (3, "c"), (4, "d")))
    gh = FakeGh(prs)
    _detect(gh)
    (issue,) = gh.open_issues()
    assert len(_notes_on(gh, 4)) == 0
    _detect(gh, trigger=4)
    assert len(_notes_on(gh, 4)) == 1
    assert len(gh.open_issues()) == 1 and gh.open_issues()[0]["number"] == issue["number"]


def test_no_account_name_is_in_a_comment() -> None:
    gh = FakeGh((_pr(1, "ada"), _pr(2, "grace", actor="hopper"), _pr(3, "edsger")))
    _detect(gh)
    written = "".join(body for number in (1, 2, 3) for body in _notes_on(gh, number))
    for name in ("ada", "grace", "hopper", "edsger"):
        assert name not in written


def test_a_main_run_whose_issue_read_fails_says_only_that() -> None:
    gh = FakeGh(issues=(_suspect_issue(),), refuse=("issues?",))
    api = _settle(gh, "success")
    assert gh.writes() == []
    assert not any("No suspected issue is open" in line for line in api.lines)
    assert any("Could not read" in line for line in api.lines)
