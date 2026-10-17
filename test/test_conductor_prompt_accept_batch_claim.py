"""The conductor prompt, the goal-conductor skill and the server agree on who drops
a placeholder bar from ``accept_batch``.

The prompt says the batch "deliberately leaves that item out" when a worker claims a
pull request while the stored ``acceptance`` still holds a placeholder. The skill
says the server omits it and flags ``acceptance_concrete: false``. Both are
statements about :func:`work_ledger.accept_batch`, so this file pins all three
together: reword one side, or change the server, and it fails here instead of the
prose and the code disagreeing silently.
"""

from __future__ import annotations

from pathlib import Path

import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, _new_item

from kiro_crew import agent
from kiro_crew import work_ledger as wl

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home

ROOT = Path(__file__).resolve().parents[1]
GOAL_CONDUCTOR_SKILL = ROOT / "src" / "kiro_crew" / "builtin_skills" / "goal-conductor" / "SKILL.md"


def _flat(text: str) -> str:
    """Collapse hard wraps, so a rewrap of the prose does not fail the pin."""
    return " ".join(text.split())


def test_the_prompt_says_the_batch_leaves_a_placeholder_item_out() -> None:
    assert "the batch deliberately leaves that item out" in _flat(agent._CONDUCTOR_SYSTEM_PROMPT)


def test_the_skill_says_the_server_flags_the_omitted_item() -> None:
    assert "acceptance_concrete: false" in GOAL_CONDUCTOR_SKILL.read_text(encoding="utf-8")


def test_the_server_leaves_a_claimed_pr_with_a_placeholder_bar_out_of_the_batch() -> None:
    """The exact case the prompt names: a worker claims a pull request while the bar
    still says ``"TBD"``. The item is ``done`` and open, yet absent, and the claim
    never becomes the bar."""
    bar = {"kind": "pr_checks", "pr": "TBD", "repo": "owner/name"}
    item_id = _new_item(acceptance=dict(bar))
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="opened it", pr=4321)

    items = wl.list_work_items(CONDUCTOR)
    (item,) = [i for i in items if i.item_id == item_id]
    assert item.status == "done" and not item.is_terminal
    assert item.acceptance == bar
    assert wl.is_acceptance_concrete(item.acceptance) is False
    assert item_id not in [entry["id"] for entry in wl.accept_batch(items)["items"]]
