"""The pending-acceptance budget: a run of ``pending`` verdicts goes overdue.

``accept_eval.py`` keeps no state between cycles, so the store stamps when an item's
run of ``pending`` verdicts began (``pending_since``) and ``is_pending_overdue``
derives, at read time, whether that run has outlasted ``PENDING_BUDGET_SECS``. These
tests pin the run itself (stamped once, kept by a later ``pending``, ended by any
other verdict), the items that never go overdue, a record from before the field, the
crew-log fold that rebuilds the stamp, and the budget each conductor-facing text
quotes.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, _new_item

from kiro_crew import mcp_work
from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import entry_types, projection, schema

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home

_REPO_ROOT = Path(__file__).resolve().parents[1]
PR_BAR = {"kind": "pr_checks", "pr": 7, "repo": "owner/name"}
START = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
SECOND = timedelta(seconds=1)


class _Clock:
    """The store's one clock (``_now_iso``), set by the test rather than read."""

    def __init__(self) -> None:
        self.now = START
        self.budget = timedelta(seconds=wl.PENDING_BUDGET_SECS)

    def stamp(self) -> str:
        return self.now.isoformat(timespec="seconds")


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    pinned = _Clock()
    monkeypatch.setattr(wl, "_now_iso", pinned.stamp)
    return pinned


def _verdict(item_id: str, verdict: str) -> wl.WorkItem:
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict=verdict)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    return item


# ── the run: stamped once, kept by a later pending, ended by any other verdict ──


def test_a_pending_run_goes_overdue_one_budget_after_its_first_verdict(clock):
    item_id = _new_item(acceptance=PR_BAR)
    first = _verdict(item_id, "pending")
    assert first.pending_since == clock.stamp()
    clock.now = START + clock.budget - timedelta(hours=1)
    item = _verdict(item_id, "pending")
    assert item.pending_since == first.pending_since, "a later pending verdict moved the run"
    assert wl.is_pending_overdue(item, now=START + clock.budget) is False
    assert wl.is_pending_overdue(item, now=START + clock.budget + SECOND) is True


@pytest.mark.parametrize("other", sorted(wl.VERDICTS - {"pending"}))
def test_any_other_verdict_ends_the_run_and_the_next_pending_starts_a_new_one(clock, other):
    item_id = _new_item(acceptance=PR_BAR)
    _verdict(item_id, "pending")
    clock.now = START + clock.budget * 2
    ended = _verdict(item_id, other)
    assert ended.pending_since is None
    assert wl.is_pending_overdue(ended, now=clock.now) is False

    clock.now = START + clock.budget * 3
    restarted = _verdict(item_id, "pending")
    assert restarted.pending_since == clock.stamp(), "the new run kept the old run's start"
    assert wl.is_pending_overdue(restarted, now=clock.now + clock.budget) is False
    assert wl.is_pending_overdue(restarted, now=clock.now + clock.budget + SECOND) is True


def test_a_new_bar_ends_the_run_it_did_not_measure(clock):
    """A run belongs to the bar it measured: the next bar's first pending starts anew."""
    item_id = _new_item(acceptance=PR_BAR)
    _verdict(item_id, "pending")
    clock.now = START + clock.budget - timedelta(hours=1)
    wl.apply_acceptance_update(CONDUCTOR, item_id, acceptance={**PR_BAR, "pr": 8})
    promoted = wl.read_work_item(CONDUCTOR, item_id)
    assert promoted is not None and promoted.pending_since is None
    assert wl.is_pending_overdue(promoted, now=START + clock.budget * 2) is False
    restarted = _verdict(item_id, "pending")
    assert restarted.pending_since == clock.stamp(), "the new bar inherited the old run"
    assert wl.is_pending_overdue(restarted, now=START + clock.budget + SECOND) is False


# ── what never goes overdue, and what always does ──────────────────────────────


def test_a_human_approval_bar_never_goes_overdue(clock):
    """It reads ``pending`` until a person answers, and is settled by asking."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    item = _verdict(item_id, "pending")
    assert item.pending_since == clock.stamp()
    assert wl.is_pending_overdue(item, now=START + clock.budget * 10) is False


def test_a_closed_item_is_never_overdue_and_an_unreadable_stamp_always_is(clock):
    item_id = _new_item(acceptance=PR_BAR)
    item = _verdict(item_id, "pending")
    unreadable = dataclasses.replace(item, pending_since="not a stamp")
    assert wl.is_pending_overdue(unreadable, now=START) is True
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="abandoned")
    closed = wl.read_work_item(CONDUCTOR, item_id)
    assert closed is not None and closed.pending_since == item.pending_since
    assert wl.is_pending_overdue(closed, now=START + clock.budget * 10) is False


def test_a_record_from_before_the_field_loads_and_is_not_overdue(clock):
    item_id = _new_item(acceptance=PR_BAR)
    _verdict(item_id, "pending")
    path = wl.item_path(CONDUCTOR, item_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("pending_since", None)
    path.write_text(json.dumps(raw), encoding="utf-8")

    old = wl.read_work_item(CONDUCTOR, item_id)
    assert old is not None and old.verdict == "pending" and old.pending_since is None
    assert wl.is_pending_overdue(old, now=START + clock.budget * 10) is False
    # Its next pending verdict starts the run then, rather than guessing an older start.
    clock.now = START + clock.budget * 10
    assert _verdict(item_id, "pending").pending_since == clock.stamp()


# ── the crew-log fold rebuilds the same stamp ─────────────────────────────────

ITEM = "it_0000abcd"
STAMP = "2026-10-01T09:00:00+00:00"


def _entry(seq: int, action: str, **fields: Any) -> schema.Entry:
    """One conductor ``work/recorded`` entry about :data:`ITEM`."""
    data = {"slot": "board", "actor": "conductor", "by": "board", "action": action}
    data.update(item_id=ITEM, **fields)
    return schema.Entry(
        type=entry_types.WORK_ENTRY_TYPE,
        seq=seq,
        time=1_790_000_000_000 + seq * 60_000,
        src="test",
        data=data,
    )


def test_the_fold_carries_a_pending_run_and_clears_it_on_another_verdict():
    state = projection._work_start()
    projection._work_step(state, _entry(1, "create", title="t"))
    assert state["items"][ITEM]["pending_since"] is None
    projection._work_step(state, _entry(2, "verdict", verdict="pending", pending_since=STAMP))
    assert state["items"][ITEM]["pending_since"] == STAMP
    projection._work_step(state, _entry(3, "verdict", verdict="pass"))
    assert state["items"][ITEM]["pending_since"] is None
    # A pending verdict recorded before the field carries none, and its cache record
    # held none either, so the rebuild agrees with the cache.
    projection._work_step(state, _entry(4, "verdict", verdict="pending"))
    assert state["items"][ITEM]["pending_since"] is None
    # A new bar ends a run in the fold as it does in the store.
    projection._work_step(state, _entry(5, "verdict", verdict="pending", pending_since=STAMP))
    projection._work_step(state, _entry(6, "accept", acceptance=PR_BAR))
    assert state["items"][ITEM]["pending_since"] is None


def test_a_baseline_entry_keeps_the_committed_pending_since():
    state = projection._work_start()
    projection._work_step(
        state,
        _entry(1, "decide", baseline=True, title="t", verdict="pending", pending_since=STAMP),
    )
    assert state["items"][ITEM]["pending_since"] == STAMP


def test_the_crew_log_accepts_a_verdict_entry_carrying_the_stamp():
    """An undeclared field is refused at append, which would undo every pending verdict."""
    data = dict(_entry(1, "verdict", verdict="pending", pending_since=STAMP).data)
    entry_types.validate_data(schema.KIND_SESSION, entry_types.WORK_ENTRY_TYPE, data)


# ── every conductor-facing text quotes the real budget ─────────────────────────


def test_the_tool_the_skill_and_the_doc_quote_the_real_budget():
    """Each text mirrors the constant rather than importing it, so each is pinned."""
    hours = f"{int(wl.PENDING_BUDGET_SECS // 3600)} hours on end"
    read = next(t for t in mcp_work._list_tools() if t["name"] == "work_ledger_read")
    assert hours in read["description"] and "pending_overdue" in read["description"]
    skill = _REPO_ROOT / "src/kiro_crew/builtin_skills/goal-conductor/SKILL.md"
    doc = _REPO_ROOT / "src/kiro_crew/docs/work-ledger.md"
    for page in (skill, doc):
        text = " ".join(page.read_text(encoding="utf-8").split())
        assert hours in text and "pending_overdue" in text, page.name
