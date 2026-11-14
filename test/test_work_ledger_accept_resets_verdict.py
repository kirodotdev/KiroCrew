"""Promoting an item's acceptance bar clears the verdict the old bar earned.

``close`` with ``state=accepted`` is refused unless the item's ``verdict`` is
``pass``. ``accept`` replaces the bar and already resets ``pending_since``
because a pending run belongs to the bar it measured, but it kept ``verdict``:
a ``pass`` recorded against the old bar still let the item close accepted, so
the new bar (for example the worker's real pull request) was never checked.
"""

from __future__ import annotations

from typing import Any

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, _new_item

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import entry_types, projection, schema

_isolated_home = work_ledger_helpers._isolated_home


def test_a_promoted_bar_must_earn_its_own_pass_before_closing_accepted():
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pass")

    wl.apply_acceptance_update(
        CONDUCTOR, item_id, acceptance={"kind": "pr_checks", "pr": 4321, "repo": "owner/name"}
    )

    assert wl.read_work_item(CONDUCTOR, item_id).verdict is None
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")


def test_a_pass_on_the_current_bar_still_closes_accepted():
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_acceptance_update(
        CONDUCTOR, item_id, acceptance={"kind": "pr_checks", "pr": 4321, "repo": "owner/name"}
    )
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pass")

    out = wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    assert out["item"].state == "accepted"


def _entry(seq: int, action: str, **fields: Any) -> schema.Entry:
    data = {"slot": "board", "actor": "conductor", "by": "board", "action": action}
    data.update(item_id="it_0000abcd", **fields)
    return schema.Entry(
        type=entry_types.WORK_ENTRY_TYPE,
        seq=seq,
        time=1_790_000_000_000 + seq * 60_000,
        src="test",
        data=data,
    )


def test_the_crew_log_fold_clears_the_verdict_on_accept_as_the_store_does():
    # A rebuild from the crew log must agree with the cache, so the fold drops the
    # old bar's verdict at the same step.
    state = projection._work_start()
    projection._work_step(state, _entry(1, "create", title="t"))
    projection._work_step(state, _entry(2, "verdict", verdict="pass"))
    assert state["items"]["it_0000abcd"]["verdict"] == "pass"
    projection._work_step(
        state, _entry(3, "accept", acceptance={"kind": "pr_checks", "pr": 7, "repo": "o/n"})
    )
    assert state["items"]["it_0000abcd"]["verdict"] is None
