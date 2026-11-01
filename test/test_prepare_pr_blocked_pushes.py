"""Tests for the PR-level seam count: pushes that replaced a BLOCKED head.

`_review_contract.blocked_push_count` reads the PR timeline and each replaced head's
review-lane check runs with GET calls only, and `pr_findings.py --rounds` prints SEAM
REPEAT / SEAM LIMIT (pr-level) from it.
"""

from __future__ import annotations

import json
import re

import pytest
from prepare_pr_rounds_helpers import CLASS_A, HEADS, SCRIPT, _contract, _disp, _load, _wire

# --- the PR-level seam trigger: pushes after a blocked head, no disposition marker needed ---

LANE = "GPT 6.1 Review"
SHA = [format(i, "x") * 40 for i in range(1, 10)]


def _force(sha: str) -> dict:
    return {"event": "head_ref_force_pushed", "commit_id": sha}


def _committed(sha: str, parent: str) -> dict:
    return {"event": "committed", "sha": sha, "parents": [{"sha": parent}]}


def _review(sha: str, state: str = "changes_requested", rid: int = 1, who: str = "alice") -> dict:
    return {
        "event": "reviewed",
        "id": rid,
        "state": state,
        "commit_id": sha,
        "user": {"login": who},
    }


class _FakeGh:
    """The `run` callable the helper takes: a timeline and per-head check runs, GET only."""

    def __init__(self, events: list[dict], checks: dict[str, list[tuple]] | None = None):
        self.events = events
        self.checks = checks or {}
        self.calls: list[list[str]] = []
        self.broken_checks: set[str] = set()
        self.broken_timeline = False

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        url = args[2]
        if "/timeline" in url:
            if self.broken_timeline:
                return 1, "", "HTTP 502"
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            return 0, json.dumps(self.events[(page - 1) * 100 : page * 100]), ""
        sha = re.search(r"/commits/([0-9a-f]+)/check-runs", url).group(1)
        if sha in self.broken_checks:
            return 1, "", "HTTP 404"
        rows = [
            {
                "name": t[0],
                "conclusion": t[1],
                "status": "completed",
                **({"id": t[2]} if t[2:] else {}),
            }
            for t in self.checks.get(sha, [])
        ]
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        return (
            0,
            json.dumps(
                {"total_count": len(rows), "check_runs": rows[(page - 1) * 100 : page * 100]}
            ),
            "",
        )

    def check_calls(self) -> int:
        return sum("/check-runs" in c[2] for c in self.calls)


def _count(gh: _FakeGh, **kw):
    return _contract().blocked_push_count("o/r", 7, gh, **kw)


def test_pushes_after_a_blocked_head_are_counted():
    # h0..h3 each fail a lane; h4 is the head now: four pushes replaced a blocked head
    gh = _FakeGh(
        [_force(h) for h in SHA[:5]],
        {h: [(LANE, "failure"), ("Backend Tests (3.12, 1)", "success")] for h in SHA[:4]},
    )
    assert _count(gh) == 4


def test_a_push_over_a_green_head_is_not_counted_so_rebases_and_flakes_add_nothing():
    gh = _FakeGh(
        [_force(h) for h in SHA[:5]],
        {
            SHA[0]: [(LANE, "failure")],
            SHA[1]: [(LANE, "success"), ("Backend Tests (3.12, 2)", "success")],
            SHA[2]: [
                ("Backend Tests (3.12, 2)", "failure"),
                ("Backend Tests (Windows) (3)", "failure"),
            ],
            SHA[3]: [(LANE, "success")],
        },
    )
    # only h0 was blocked; a red test shard on h2 is a flake or a build, not a lane verdict
    assert _count(gh) == 1


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "skipped", "neutral", None])
def test_only_a_failed_lane_blocks_a_head(conclusion):
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: [(LANE, conclusion)]})
    assert _count(gh) == 0


@pytest.mark.parametrize(
    "name",
    ["Opus 5.5 Review", "GPT 6.1 Review", "Design Review", "UX Review", "First Principles Review"],
)
def test_each_review_lane_blocks_a_head(name):
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: [(name, "failure")]})
    assert _count(gh) == 1


@pytest.mark.parametrize(
    "name",
    [
        "Sensitive Change Review",
        "Security Scope Review",
        "Backend Tests (3.12, 2)",
        "GPT 6.1 Review (same-repo lane, not applicable to forks)",
        "Reviewdog",
    ],
)
def test_a_check_that_is_not_a_verdict_lane_blocks_nothing(name):
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: [(name, "failure")]})
    assert _count(gh) == 0


def test_a_changes_requested_review_on_a_head_blocks_it_without_a_check_call():
    gh = _FakeGh([_force(SHA[0]), _review(SHA[0]), _force(SHA[1])])
    assert _count(gh) == 1
    assert gh.check_calls() == 0, "a blocking review settles the head; no check-run read is needed"


@pytest.mark.parametrize("state", ["approved", "commented", "dismissed"])
def test_other_review_states_block_nothing(state):
    gh = _FakeGh([_force(SHA[0]), _review(SHA[0], state), _force(SHA[1])])
    assert _count(gh) == 0


def test_a_dismissed_changes_requested_review_no_longer_blocks():
    dismissed = {"event": "review_dismissed", "dismissed_review": {"review_id": 5}}
    gh = _FakeGh([_force(SHA[0]), _review(SHA[0], rid=5), dismissed, _force(SHA[1])])
    assert _count(gh) == 0


def _dismissed(rid: int) -> dict:
    return {"event": "review_dismissed", "dismissed_review": {"review_id": rid}}


def test_a_request_then_an_approval_by_the_same_reviewer_on_the_same_head_is_not_a_block():
    events = [_force(SHA[0]), _review(SHA[0], rid=1), _review(SHA[0], "approved", rid=2)]
    assert _count(_FakeGh(events + [_force(SHA[1])])) == 0


def test_three_heads_approved_after_a_request_do_not_trip_the_seam_count():
    events = []
    for i in range(3):
        events += [_force(SHA[i]), _review(SHA[i], rid=i), _review(SHA[i], "approved", rid=10 + i)]
    assert _count(_FakeGh(events + [_force(SHA[3])])) == 0


def test_a_comment_after_a_request_does_not_clear_it():
    events = [_force(SHA[0]), _review(SHA[0], rid=1), _review(SHA[0], "commented", rid=2)]
    assert _count(_FakeGh(events + [_force(SHA[1])])) == 1


def test_a_request_after_an_approval_blocks_again():
    events = [_force(SHA[0]), _review(SHA[0], "approved", rid=1), _review(SHA[0], rid=2)]
    assert _count(_FakeGh(events + [_force(SHA[1])])) == 1


def test_an_approval_clears_only_the_approving_reviewers_request():
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1, who="alice"),
        _review(SHA[0], rid=2, who="bob"),
        _review(SHA[0], "approved", rid=3, who="alice"),
        _force(SHA[1]),
    ]
    assert _count(_FakeGh(events)) == 1, "bob's request still stands"
    events.insert(4, _review(SHA[0], "approved", rid=4, who="bob"))
    assert _count(_FakeGh(events)) == 0, "both reviewers approved"


def test_reviewer_logins_are_compared_without_case_and_a_missing_user_is_one_reviewer():
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1, who="Alice"),
        _review(SHA[0], "approved", rid=2, who="alice"),
        _force(SHA[1]),
    ]
    assert _count(_FakeGh(events)) == 0
    ghost = {"event": "reviewed", "id": 3, "state": "changes_requested", "commit_id": SHA[1]}
    assert _count(_FakeGh(events + [ghost, _force(SHA[2])])) == 1


def test_an_approval_of_another_head_does_not_clear_a_request():
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1),
        _force(SHA[1]),
        _review(SHA[1], "approved", rid=2),
        _force(SHA[2]),
    ]
    assert _count(_FakeGh(events)) == 1, "h0 stayed blocked when h1 was pushed over it"


def test_a_dismissal_clears_only_the_dismissed_review():
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1, who="alice"),
        _review(SHA[0], rid=2, who="bob"),
        _dismissed(1),
        _force(SHA[1]),
    ]
    assert _count(_FakeGh(events)) == 1
    # an older request that is dismissed does not clear the same reviewer's newer one
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1),
        _review(SHA[0], rid=2),
        _dismissed(1),
        _force(SHA[1]),
    ]
    assert _count(_FakeGh(events)) == 1
    assert _count(_FakeGh(events[:3] + [_dismissed(2), _force(SHA[1])])) == 0
    # a dismissal that names no review clears nothing
    nameless = {"event": "review_dismissed", "dismissed_review": {}}
    assert _count(_FakeGh([_force(SHA[0]), _review(SHA[0]), nameless, _force(SHA[1])])) == 1


def test_a_green_rerun_of_a_failed_lane_clears_the_head_but_a_later_failure_does_not():
    pair = [(LANE, "failure", 10), (LANE, "success", 11)]
    assert _count(_FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: pair})) == 0
    assert _count(_FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: pair[::-1]})) == 0
    later = [(LANE, "success", 10), (LANE, "failure", 11)]
    assert _count(_FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: later})) == 1
    # a rerun is per lane: another lane's failure stands
    other = [("Design Review", "failure", 12)]
    assert _count(_FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: pair + other})) == 1


def test_a_stale_failure_on_one_page_and_its_green_rerun_on_the_next_clear_the_head():
    filler = [("Backend Tests ({})".format(i), "success") for i in range(98)]
    rows = [(LANE, "failure", 10)] + filler + [(LANE, "failure", 5), (LANE, "success", 11)]
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: rows})
    assert _count(gh) == 0
    assert gh.check_calls() == 2


def test_a_review_of_the_current_head_is_not_a_push():
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1]), _review(SHA[1])])
    assert _count(gh) == 0


def test_a_commit_on_top_of_the_head_is_a_push_and_a_replaced_commit_is_not():
    events = [
        _committed(SHA[0], "0" * 40),  # the first head
        _committed(SHA[1], SHA[0]),  # a plain push on top
        _force(SHA[2]),  # rewrote the branch
        _committed(SHA[3], "0" * 40),  # a commit the force push replaced: not on the head
        _committed(SHA[4], SHA[3]),  # and its child: still not on the head
    ]
    gh = _FakeGh(events, {h: [(LANE, "failure")] for h in SHA[:5]})
    assert _count(gh) == 2, "h0 -> h1 and h1 -> h2 each replaced a blocked head"


def test_a_head_named_twice_in_a_row_is_one_head():
    gh = _FakeGh(
        [_force(SHA[0]), _force(SHA[0]), _force(SHA[1])],
        {SHA[0]: [(LANE, "failure")]},
    )
    assert _count(gh) == 1


def test_a_multi_commit_push_counts_once():
    # h1 and h2 land together; only the tip h2 ran CI, so h1 was never judged
    events = [_force(SHA[0]), _committed(SHA[1], SHA[0]), _committed(SHA[2], SHA[1])]
    gh = _FakeGh(events, {SHA[0]: [(LANE, "failure")], SHA[2]: [(LANE, "failure")]})
    assert _count(gh) == 1


def test_since_counts_only_the_pushes_after_that_head():
    gh = _FakeGh([_force(h) for h in SHA[:6]], {h: [(LANE, "failure")] for h in SHA[:5]})
    assert _count(gh) == 5
    assert _count(gh, since=SHA[2]) == 3
    assert _count(gh, since=SHA[2][:9]) == 3, "a short spelling names the same head"
    assert _count(gh, since=SHA[5]) == 0
    assert _count(gh, since="e" * 40) == 5, "a head that is not on the PR restarts nothing"


def test_stop_at_ends_the_reads_early():
    gh = _FakeGh([_force(h) for h in SHA[:9]], {h: [(LANE, "failure")] for h in SHA[:8]})
    assert _count(gh, stop_at=3) == 3
    assert gh.check_calls() == 3


def test_every_call_is_a_plain_get():
    gh = _FakeGh([_force(h) for h in SHA[:3]], {SHA[0]: [(LANE, "failure")]})
    _count(gh)
    assert gh.calls
    for call in gh.calls:
        assert call[:2] == ["gh", "api"] and len(call) == 3
        assert call[2].startswith("repos/o/r/")


def test_the_timeline_is_read_across_pages_and_the_checks_too():
    filler = [{"event": "labeled"} for _ in range(150)]
    gh = _FakeGh(filler + [_force(SHA[0]), _force(SHA[1])])
    assert _count(gh) == 0
    assert sum("/timeline" in c[2] for c in gh.calls) == 2
    # a lane failure that sits on the second page of a head's check runs
    rows = [("Backend Tests ({})".format(i), "success") for i in range(130)] + [(LANE, "failure")]
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: rows})
    assert _count(gh) == 1


def test_an_unreadable_timeline_or_check_read_is_none_not_zero():
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])])
    gh.broken_timeline = True
    assert _count(gh) is None
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])])
    gh.broken_checks.add(SHA[0])
    assert _count(gh) is None
    assert _contract().blocked_push_count("", 7, gh) is None


def test_a_pr_with_no_push_history_counts_zero():
    assert _count(_FakeGh([])) == 0
    assert _count(_FakeGh([_force(SHA[0])])) == 0


def test_the_pr_level_thresholds_are_three_and_five():
    c = _contract()
    assert (c.BLOCKED_PUSH_REPEAT, c.BLOCKED_PUSH_LIMIT) == (3, 5)


def _replies(n: int) -> list[dict]:
    """Writer dispositions for n distinct heads, none of them a fix of a shared span."""
    return [
        _disp(i + 1, "gpt", HEADS[i], "{:011d}{}".format(i, "c"), word="rebutted") for i in range(n)
    ]


@pytest.mark.parametrize("pushes", [3, 4])
def test_three_blocked_pushes_print_seam_repeat_pr_level_and_exit_30(monkeypatch, capsys, pushes):
    mod = _load()
    # four rounds disposed and no span fixed twice, so neither the cadence nor a marker fires
    _wire(mod, monkeypatch, _replies(4), pushes=pushes)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "blocked pushes (pr-level): {}".format(pushes) in out
    assert "SEAM REPEAT (pr-level)" in out and "SEAM LIMIT" not in out
    assert "The next push would be its {}".format("4th" if pushes == 3 else "5th") in out
    assert "No one needs to approve; continue." in out


@pytest.mark.parametrize("pushes", [5, 9])
def test_five_blocked_pushes_print_seam_limit_pr_level_and_only_narrow_or_defer(
    monkeypatch, capsys, pushes
):
    mod = _load()
    _wire(mod, monkeypatch, _replies(4), pushes=pushes)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM LIMIT (pr-level)" in out and "SEAM REPEAT" not in out
    assert "narrow the scope, or defer with an issue" in out
    assert "remove the second writer" not in out


def test_two_blocked_pushes_or_an_unreadable_timeline_trigger_nothing(monkeypatch, capsys):
    mod = _load()
    _wire(mod, monkeypatch, _replies(4), pushes=2)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    out = capsys.readouterr().out
    assert "blocked pushes (pr-level): 2" in out and "SEAM" not in out
    _wire(mod, monkeypatch, _replies(4), pushes=None)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    out = capsys.readouterr().out
    assert "blocked pushes" not in out and "SEAM" not in out


def test_a_pr_with_no_disposition_marker_at_all_still_trips_the_pr_level_seam(monkeypatch, capsys):
    mod = _load()
    _wire(mod, monkeypatch, [], pushes=3)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "(no disposition records yet - this is round 0)" in out
    assert "SEAM REPEAT (pr-level)" in out


def test_a_marker_seam_wins_and_the_push_count_is_not_read(monkeypatch, capsys):
    mod = _load()
    comments = [_disp(i + 1, "gpt", HEADS[i], "aaaaaaaaaaa1", CLASS_A) for i in range(2)]
    comments += [
        _disp(9, "gpt", HEADS[2], "cccccccccc21"),
        _disp(10, "gpt", HEADS[3], "cccccccccc22"),
    ]
    _wire(mod, monkeypatch, comments)
    reads: list = []
    monkeypatch.setattr(mod, "pr_blocked_pushes", lambda *a, **k: reads.append(a) or 9)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM REPEAT span=aaaaaaaaaaa1" in out
    assert "(pr-level)" not in out and not reads


def test_a_recorded_resolution_restarts_the_pr_level_count_from_that_round(monkeypatch, capsys):
    mod = _load()
    comments = _replies(4)
    comments[2] = _disp(
        3, "gpt", HEADS[2], "{:011d}{}".format(2, "c"), "resolution: narrowed -- x\n"
    )
    _wire(mod, monkeypatch, comments)
    seen: list = []
    monkeypatch.setattr(
        mod, "pr_blocked_pushes", lambda repo, number, since="": seen.append(since) or 2
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert seen == [HEADS[2]], "the count starts after the head the resolution was recorded on"
    seen.clear()
    _wire(mod, monkeypatch, _replies(4))
    monkeypatch.setattr(
        mod, "pr_blocked_pushes", lambda repo, number, since="": seen.append(since) or 2
    )
    mod.rounds_view("o/r", 7, "f" * 40, {})
    assert seen == [""]


def test_a_resolution_in_a_spanless_disposition_restarts_the_pr_level_count(monkeypatch, capsys):
    mod = _load()
    comments = _replies(4)
    comments[3] = _disp(4, "design", HEADS[3], None, "resolution: narrowed -- x\n")
    _wire(mod, monkeypatch, comments)
    seen: list = []
    monkeypatch.setattr(
        mod, "pr_blocked_pushes", lambda repo, number, since="": seen.append(since) or 0
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert seen == [HEADS[3]], "a record that claims no span still carries the resolution"
    assert "SEAM" not in capsys.readouterr().out


def test_the_round_records_a_resolution_whether_or_not_it_names_a_span():
    c = _contract()
    spanned = {"comment_id": 1, "head": HEADS[0], "spans": ["aaaaaaaaaaa1"], "malformed": False}
    spanless = {"comment_id": 2, "head": HEADS[1], "spans": [], "malformed": False}
    none = {"comment_id": 3, "head": HEADS[2], "spans": [], "malformed": False}
    bodies = {
        1: "**fixed** -- x\nresolution: removed -- gone\n",
        2: "**rebutted** -- x\nresolution: deferred #4711 pinned-by t\n",
        3: "**rebutted** -- x\n",
    }
    order, rounds = c.disposition_rounds([spanned, spanless, none], bodies)
    assert [rounds[k]["resolution"] for k in order] == ["removed", "deferred", ""]
    assert rounds[order[0]]["resolved"] == {"aaaaaaaaaaa1": "removed"}
    assert rounds[order[1]]["resolved"] == {}, "the per-span map the fix count reads is unchanged"


def test_the_head_a_first_force_push_replaced_is_recovered_from_a_review_before_it():
    # A was blocked by a review, then pushes replaced A, B and C: three pushes over blocked heads
    events = [
        _review(SHA[0], rid=1),
        _force(SHA[1]),
        _review(SHA[1], rid=2),
        _force(SHA[2]),
        _review(SHA[2], rid=3),
        _force(SHA[3]),
    ]
    assert _count(_FakeGh(events)) == 3


def test_a_lane_failure_on_the_recovered_first_head_counts():
    # the review only names A; the lane failure on A is what blocked it
    events = [_review(SHA[0], "commented", rid=1), _force(SHA[1])]
    assert _count(_FakeGh(events, {SHA[0]: [(LANE, "failure")]})) == 1
    assert _count(_FakeGh(events, {SHA[0]: [(LANE, "success")]})) == 0


def test_a_review_of_a_head_already_known_adds_no_head():
    events = [
        _committed(SHA[0], "0" * 40),
        _review(SHA[0], "commented", rid=1),
        _force(SHA[1]),
        _review(SHA[1], "commented", rid=2),
        _force(SHA[2]),
    ]
    gh = _FakeGh(events, {SHA[0]: [(LANE, "failure")], SHA[1]: [(LANE, "failure")]})
    assert _count(gh) == 2


def test_a_first_head_with_no_commit_event_and_no_review_stays_unseen():
    # the timeline names only the head a force push created; nothing else names A
    gh = _FakeGh([_force(SHA[1]), _force(SHA[2])], {SHA[0]: [(LANE, "failure")]})
    assert _count(gh) == 0


def test_three_pushes_over_blocked_heads_print_the_strategy_switch_when_a_review_named_the_first(
    monkeypatch, capsys
):
    mod = _load()
    events = [
        _review(SHA[0], rid=1),
        _force(SHA[1]),
        _review(SHA[1], rid=2),
        _force(SHA[2]),
        _review(SHA[2], rid=3),
        _force(SHA[3]),
    ]
    gh = _FakeGh(events)
    _wire(mod, monkeypatch, _replies(4))
    real = mod._review_contract.blocked_push_count
    monkeypatch.setattr(
        mod, "pr_blocked_pushes", lambda repo, number, since="": real(repo, number, gh, since=since)
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "blocked pushes (pr-level): 3" in out and "SEAM REPEAT (pr-level)" in out


def test_a_head_the_pr_returned_to_is_taken_at_its_last_visit():
    # A, B, C, D, then a force push back to A: a reset recorded on A starts after that push
    events = [_force(h) for h in (SHA[0], SHA[1], SHA[2], SHA[3], SHA[0])]
    checks = {h: [(LANE, "failure")] for h in SHA[:4]}
    assert _count(_FakeGh(events, checks)) == 4
    assert _count(_FakeGh(events, checks), since=SHA[0]) == 0
    assert _count(_FakeGh(events + [_force(SHA[4])], checks), since=SHA[0]) == 1
    assert _count(_FakeGh(events, checks), since=SHA[1]) == 3


def test_the_real_wrapper_reads_through_the_run_function(monkeypatch):
    mod = _load()
    gh = _FakeGh([_force(h) for h in SHA[:4]], {h: [(LANE, "failure")] for h in SHA[:3]})
    monkeypatch.setattr(mod, "run", gh)
    assert mod.pr_blocked_pushes("o/r", 7) == 3
    assert mod.pr_blocked_pushes("o/r", 7, SHA[1]) == 2
    assert (mod.BLOCKED_PUSH_REPEAT, mod.BLOCKED_PUSH_LIMIT) == (3, 5)


def test_the_seam_protocol_reference_documents_the_pr_level_count():
    text = (SCRIPT.parent.parent / "references" / "seam-protocol.md").read_text(encoding="utf-8")
    assert "## PR-level count" in text
    assert "SEAM REPEAT (pr-level)" in text and "SEAM LIMIT (pr-level)" in text
    assert "3 pushes" in text and "5 pushes" in text
