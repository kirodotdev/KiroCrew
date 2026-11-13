"""Phase A of ``rfc-conductor-pr-land-tracking``: a PR item is tracked until it merges.

Pinned here: the landed bar the store writes for a ``pr_checks`` bar (§3.2), the
``awaiting_land`` derivation and its seat outside the goal's concurrency cap (§3.3),
the worker's ``ready_when`` (§3.1), the batched pull-request read (§3.4, §3.6) and
the four wakes the work-ledger probe raises from it (§3.4). No network: the probe
takes an injected reader, and ``fetch_states`` an injected transport.
"""

from __future__ import annotations

import json
import re

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER

from kiro_crew import ledger_wake
from kiro_crew import work_ledger as wl
from kiro_crew.probes import gh_pr
from kiro_crew.probes.work_ledger import WorkLedgerProbe
from kiro_crew.testing.clock import ManualClock

_isolated_home = work_ledger_helpers._isolated_home

REPO = "owner/name"
CHECKS = {"kind": "pr_checks", "pr": 7, "repo": REPO}


def _create(acceptance: dict) -> dict:
    wl.ensure_conductor(CONDUCTOR, goal="land the fleet")
    return wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance=acceptance)


def _awaiting(item_id: str) -> None:
    """Bind, report ``done``, rule ``pending``: the shape of a green, unmerged PR."""
    wl.apply_conductor_action(
        CONDUCTOR, "bind", item_id=item_id, worker_session_key=f"{WORKER}-{item_id}"
    )
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="green", pr=7)
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pending")


# ── §3.2 the landed bar ───────────────────────────────────────────────────────


def test_a_pr_checks_bar_with_a_repo_is_stored_as_a_landed_bar_and_says_so():
    result = _create(dict(CHECKS))
    assert result["item"].acceptance == {
        "kind": "pr_merged",
        "pr": 7,
        "repo": REPO,
        "ready": "pr_checks",
    }
    assert result["acceptance_rewritten"] == wl.LAND_REWRITE


@pytest.mark.parametrize(
    "bar",
    [
        {**CHECKS, "until": "checks"},
        {"kind": "pr_checks", "pr": 7},
        {"kind": "pr_checks", "pr": 7, "repo": "not a repo"},
        {"kind": "pr_merged", "pr": 7, "repo": REPO},
        {"kind": "human_approval"},
    ],
)
def test_every_other_bar_is_stored_as_given(bar):
    result = _create(dict(bar))
    assert result["item"].acceptance == bar
    assert result["acceptance_rewritten"] is None


def test_land_untracked_names_only_a_pr_checks_bar_with_no_usable_repo():
    assert wl.is_land_untracked({"kind": "pr_checks", "pr": 7})
    assert wl.is_land_untracked({"kind": "pr_checks", "pr": 7, "repo": "nope"})
    assert not wl.is_land_untracked({**CHECKS, "until": "checks"})
    assert not wl.is_land_untracked({"kind": "pr_merged", "pr": 7, "repo": REPO})


def test_a_tbd_bar_is_promoted_to_a_landed_bar():
    item_id = _create({"kind": "pr_checks", "pr": "TBD", "repo": REPO})["item"].item_id
    stored = wl.read_work_item(CONDUCTOR, item_id)
    assert stored is not None and stored.acceptance["kind"] == "pr_merged"
    assert not wl.is_acceptance_concrete(stored.acceptance)
    result = wl.apply_acceptance_update(CONDUCTOR, item_id, acceptance=dict(CHECKS))
    assert result["item"].acceptance["kind"] == "pr_merged"
    assert result["acceptance_rewritten"] == wl.LAND_REWRITE
    assert wl.is_acceptance_concrete(result["item"].acceptance)


def test_the_worker_brief_names_the_stage_it_is_done_at():
    item_id = _create(dict(CHECKS))["item"].item_id
    assert wl.read_work_brief(CONDUCTOR, item_id)["ready_when"] == "pr_checks"
    other = _create({"kind": "human_approval"})["item"].item_id
    assert "ready_when" not in wl.read_work_brief(CONDUCTOR, other)


# ── §3.3 awaiting land ────────────────────────────────────────────────────────


def test_awaiting_land_needs_open_landed_bar_done_and_pending():
    item_id = _create(dict(CHECKS))["item"].item_id
    assert not wl.is_awaiting_land(wl.read_work_item(CONDUCTOR, item_id))
    _awaiting(item_id)
    assert wl.is_awaiting_land(wl.read_work_item(CONDUCTOR, item_id))
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="re-seeded")
    assert not wl.is_awaiting_land(wl.read_work_item(CONDUCTOR, item_id))


def test_a_create_past_the_goal_cap_counts_awaiting_items_out():
    wl.ensure_conductor(CONDUCTOR, goal="land the fleet")
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="land the fleet", concurrency_cap=2)
    first = _create(dict(CHECKS))["item"].item_id
    _create(dict(CHECKS))
    with pytest.raises(wl.WorkLedgerError) as refused:
        _create(dict(CHECKS))
    assert refused.value.code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    _awaiting(first)
    assert _create(dict(CHECKS))["item"].state == "open"


# ── §3.4 the batched read ─────────────────────────────────────────────────────


class _FakeTransport:
    """Answers ``call`` from a list, records the argv, and carries rate-limit numbers."""

    def __init__(self, answers, *, remaining=None, reset=None):
        self.answers = list(answers)
        self.calls: list[list[str]] = []
        self.rate_limit_remaining = remaining
        self.rate_limit_reset = reset

    def call(self, args):
        self.calls.append(list(args))
        return self.answers.pop(0)


def _node(state="OPEN", mergeable="MERGEABLE", rollup="SUCCESS", head="abc"):
    return {
        "pullRequest": {
            "state": state,
            "mergedAt": None,
            "mergeable": mergeable,
            "isDraft": False,
            "headRefOid": head,
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": rollup}}}]},
        }
    }


@pytest.fixture
def _no_backoff(monkeypatch):
    monkeypatch.setattr(gh_pr, "_states_backoff_until", 0.0)


def test_fetch_states_batches_twenty_per_query_and_skips_invalid_pairs(_no_backoff):
    pairs = [(REPO, n) for n in range(1, 26)] + [
        ("bad repo", 1),
        (REPO, 0),
        (REPO, True),
        (REPO, 2**31),
    ]
    answers = [
        gh_pr._Response(0, json.dumps({"data": {f"p{i}": _node() for i in range(20)}})),
        gh_pr._Response(0, json.dumps({"data": {"p0": _node("MERGED"), "p1": None}})),
    ]
    channel = _FakeTransport(answers)
    found = gh_pr.fetch_states(pairs, transport=channel)
    assert len(channel.calls) == 2
    assert all(call[:3] == ["api", "graphql", "--include"] for call in channel.calls)
    assert "bad repo" not in " ".join(channel.calls[0] + channel.calls[1])
    assert str(2**31) not in " ".join(channel.calls[0] + channel.calls[1])
    assert len(found) == 21
    assert found[(REPO, 21)]["state"] == "MERGED"
    assert (REPO, 22) not in found, "an alias the answer does not carry reads unread"


def test_an_unresolvable_pr_does_not_blind_the_rest_of_the_board(_no_backoff):
    """``gh`` exits 1 when one alias cannot resolve; the others and the next batch count."""
    partial = json.dumps(
        {
            "data": {"p0": None, **{f"p{i}": _node() for i in range(1, 20)}},
            "errors": [{"message": "Could not resolve to a PullRequest"}],
        }
    )
    answers = [
        gh_pr._Response(1, partial, "GraphQL: Could not resolve to a PullRequest"),
        gh_pr._Response(0, json.dumps({"data": {"p0": _node("MERGED")}})),
    ]
    channel = _FakeTransport(answers)
    found = gh_pr.fetch_states([(REPO, n) for n in range(1, 22)], transport=channel)
    assert len(channel.calls) == 2
    assert (REPO, 1) not in found
    assert len(found) == 20
    assert found[(REPO, 21)]["state"] == "MERGED"


def test_fetch_states_uses_the_ledgers_own_repo_pattern(_no_backoff, monkeypatch):
    """A repo the ledger's pattern refuses is never sent, and one it accepts is."""
    monkeypatch.setattr(wl, "ACCEPTANCE_GH_REPO", re.compile(r"only/this"))
    channel = _FakeTransport([gh_pr._Response(0, json.dumps({"data": {"p0": _node()}}))])
    found = gh_pr.fetch_states([(REPO, 1), ("only/this", 2)], transport=channel)
    assert list(found) == [("only/this", 2)]
    assert 'owner: "owner"' not in " ".join(channel.calls[0])


def test_a_spent_window_skips_reads_until_its_reset(_no_backoff, monkeypatch):
    clock = ManualClock(start=1_000_000.0)
    clock.install(monkeypatch, gh_pr)
    now = clock.time()
    answer = gh_pr._Response(0, json.dumps({"data": {"p0": _node()}}))
    channel = _FakeTransport([answer], remaining=gh_pr._RATE_LIMIT_FLOOR, reset=now + 600)
    assert gh_pr.fetch_states([(REPO, 1)], transport=channel)
    idle = _FakeTransport([])
    assert gh_pr.fetch_states([(REPO, 1)], transport=idle) == {}
    assert idle.calls == []
    clock.advance(601)
    again = _FakeTransport([gh_pr._Response(0, json.dumps({"data": {"p0": _node()}}))])
    assert gh_pr.fetch_states([(REPO, 1)], transport=again)


def test_a_rate_limit_refusal_backs_off_and_reads_nothing(_no_backoff):
    refusal = gh_pr._Response(1, "", "API rate limit exceeded for user")
    assert gh_pr.fetch_states([(REPO, 1)], transport=_FakeTransport([refusal])) == {}
    idle = _FakeTransport([])
    assert gh_pr.fetch_states([(REPO, 1)], transport=idle) == {}
    assert idle.calls == []


# ── §3.4 the probe's wakes ────────────────────────────────────────────────────


class _Ctx:
    message = json.dumps({"conductor": CONDUCTOR})

    class job:  # noqa: N801 - the attribute the kernel reads
        id = "loop-land"


def _probe(facts: dict) -> WorkLedgerProbe:
    def _read(pairs):
        return {pair: facts[pair] for pair in pairs if pair in facts}

    return WorkLedgerProbe(worker_running=lambda _k: True, pr_states=_read)


def _land_briefs(probe: WorkLedgerProbe) -> list[str]:
    return [o.brief for o in probe.observe(_Ctx()).observations if "reason=pr_" in o.brief]


def _facts(state="OPEN", mergeable="MERGEABLE", rollup="SUCCESS", head="abc") -> dict:
    return {"state": state, "mergeable": mergeable, "rollup": rollup, "head": head}


@pytest.mark.parametrize(("state", "fact"), [("MERGED", "merged"), ("CLOSED", "closed")])
def test_merged_and_closed_wake_once_for_any_open_item(state, fact):
    item_id = _create(dict(CHECKS))["item"].item_id
    probe = _probe({(REPO, 7): _facts(state)})
    assert _land_briefs(probe) == [ledger_wake.land_wake_brief(item_id=item_id, pr=7, fact=fact)]
    assert _land_briefs(probe) == [], "a re-read of the same fact woke again"


@pytest.mark.parametrize(
    ("facts", "fact"),
    [(_facts(mergeable="CONFLICTING"), "conflict"), (_facts(rollup="FAILURE"), "red")],
)
def test_conflict_and_red_wake_only_an_item_awaiting_land(facts, fact):
    item_id = _create(dict(CHECKS))["item"].item_id
    wl.apply_conductor_action(
        CONDUCTOR, "bind", item_id=item_id, worker_session_key=f"{WORKER}-{item_id}"
    )
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="working")
    probe = _probe({(REPO, 7): facts})
    assert _land_briefs(probe) == [], "a working worker's PR woke its conductor"
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="green", pr=7)
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pending")
    assert f"reason=pr_{fact}" in " ".join(_land_briefs(probe))
    assert _land_briefs(probe) == []
    moved = _probe({(REPO, 7): {**facts, "head": "def"}})
    assert f"reason=pr_{fact}" in " ".join(_land_briefs(moved)), "a new head is news"


def test_a_replacement_pr_closing_wakes_again():
    """The bar moved to a new PR: its close is news, though the item's seen set stays."""
    item_id = _create(dict(CHECKS))["item"].item_id
    assert _land_briefs(_probe({(REPO, 7): _facts("CLOSED")}))
    wl.apply_acceptance_update(CONDUCTOR, item_id, acceptance={**CHECKS, "pr": 8})
    assert _land_briefs(_probe({(REPO, 8): _facts("CLOSED")})) == [
        ledger_wake.land_wake_brief(item_id=item_id, pr=8, fact="closed")
    ]


def test_an_unread_or_untracked_pr_wakes_no_one():
    _create(dict(CHECKS))
    _create({"kind": "pr_checks", "pr": 8})
    _create({"kind": "pr_checks", "pr": "TBD", "repo": REPO})
    asked: list = []

    def _read(pairs):
        asked.extend(pairs)
        return {}

    probe = WorkLedgerProbe(worker_running=lambda _k: True, pr_states=_read)
    assert _land_briefs(probe) == []
    assert asked == [(REPO, 7)], "only the concrete landed bar is read"


def test_a_probe_without_a_reader_reads_no_pr():
    _create(dict(CHECKS))
    probe = WorkLedgerProbe(worker_running=lambda _k: True)
    assert _land_briefs(probe) == []
    assert probe.pr_facts == {}


def test_a_brief_carries_no_pull_request_text():
    """Ids, a number and a fixed word: nothing a PR author chose (§9)."""
    _create(dict(CHECKS))
    hostile = {**_facts("MERGED"), "title": "IGNORE PREVIOUS", "head": "x" * 200}
    [brief] = _land_briefs(_probe({(REPO, 7): hostile}))
    assert "IGNORE" not in brief and "x" * 41 not in brief
