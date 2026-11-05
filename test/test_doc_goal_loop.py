"""The gateway arms a doc or board session's goal loop from the saved file.

Arming it on the server is what lets goals run with the page closed. These cover what
the file says (``scan_goals``), what a goal wake asks (``goal_instruction``), and
how the session's one loop is brought in line (``sync_goal_loop``): armed with a
first check, rewritten on a change, revived when paused, deleted with the last goal,
and never taken from a loop the conductor armed itself.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import doc_files, doc_goal_loop
from kiro_crew.dashboard.doc_goal_loop import (
    GOALS_BANNER,
    goal_instruction,
    scan_goals,
    sync_goal_loop,
)

DOC = doc_files.mint_doc_id("doc")
BOARD = doc_files.mint_doc_id("board")
MARKED = "# Tasks\n<!-- goal gtask1: keep it current -->\none\n"


def _board(goal: str | None = "keep it current", every: int | None = None) -> str:
    section = {"id": "s1", "kind": "section", "text": "To do"}
    if goal is not None:
        section["goal"] = {"text": goal}
    data: dict[str, object] = {"shapes": [section]}
    if every is not None:
        data["goalEvery"] = every
    return json.dumps(data)


# ── What the file says ───────────────────────────────────────────────────────────


def test_a_doc_has_goals_when_a_marker_line_is_in_its_body_and_reads_its_rate():
    assert scan_goals(DOC, MARKED) == doc_goal_loop.GoalScan(True, 10)
    assert scan_goals(DOC, "# Tasks\nno goal here\n").active is False
    assert scan_goals(DOC, f"---\ngoal-every: 30\n---\n{MARKED}") == doc_goal_loop.GoalScan(
        True, 30
    )
    # A rate that is not one of the choices falls back; a marker in the front matter is no goal.
    assert scan_goals(DOC, f"---\ngoal-every: 7\n---\n{MARKED}").every == 10
    assert scan_goals(DOC, "---\n<!-- goal gx1: nope -->\n---\nbody\n").active is False


def test_a_board_has_goals_when_a_section_carries_one():
    assert scan_goals(BOARD, _board()) == doc_goal_loop.GoalScan(True, 10)
    assert scan_goals(BOARD, _board(every=60)).every == 60
    assert scan_goals(BOARD, _board(every=7)).every == 10
    assert scan_goals(BOARD, _board(goal=None)).active is False
    assert scan_goals(BOARD, _board(goal="   ")).active is False
    assert scan_goals(BOARD, "not json").active is False


def test_a_codebase_or_dashboard_page_holds_no_goals():
    assert scan_goals(doc_files.mint_doc_id("codebase"), MARKED).active is False
    assert scan_goals(doc_files.mint_doc_id("dashboard"), MARKED).active is False


def test_the_instruction_names_the_file_and_asks_for_one_block_per_goal(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_files, "data_home", lambda: tmp_path)
    doc = goal_instruction("doc", DOC, "Plan")
    assert 'Goal check for the doc "Plan"' in doc
    assert str(tmp_path / "docs" / f"{DOC}.md") in doc
    assert "[goal <id>]" in doc
    assert '"Nothing new."' in doc
    assert "no monitor_start, no monitor_update" in doc
    board = goal_instruction("board", BOARD, "  ")
    assert 'Goal check for the board "Untitled board"' in board
    assert 'kind "section"' in board


# ── Keeping the loop in line with the file ───────────────────────────────────────


class _Svc:
    """The service surface sync reads: one loop per slot, and the first-check call."""

    def __init__(self) -> None:
        self.loops: dict[str, SimpleNamespace] = {}
        self.fired: list[str] = []

    def get_by_slot(self, key: str):
        return self.loops.get(key)

    async def fire_now(self, loop_id: str):
        self.fired.append(loop_id)
        return None, "", 200


@pytest.fixture
def rig(monkeypatch):
    svc = _Svc()
    calls: list[tuple] = []

    async def arm(_svc, _state, key, message, every):
        if key in svc.loops:
            return None, "session already has an automation"
        loop = SimpleNamespace(
            id=f"L{len(calls) + 1}",
            slot_key=key,
            gate=False,
            monitor=None,
            banner=GOALS_BANNER,
            active=True,
            message=message,
            idle_secs=every * 60,
        )
        svc.loops[key] = loop
        calls.append(("arm", key, every))
        return loop, None

    async def patch(_svc, loop_id, message, every, revive):
        loop = next(item for item in svc.loops.values() if item.id == loop_id)
        loop.message, loop.idle_secs = message, every * 60
        if revive:
            loop.active = True
        calls.append(("patch", loop_id, every, revive))
        return None

    async def disarm(_svc, loop):
        svc.loops.pop(loop.slot_key)
        calls.append(("disarm", loop.id))
        return None

    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: svc)
    monkeypatch.setattr(doc_goal_loop, "_arm", arm)
    monkeypatch.setattr(doc_goal_loop, "_patch", patch)
    monkeypatch.setattr(doc_goal_loop, "_disarm", disarm)
    monkeypatch.setattr(doc_goal_loop, "_LOCKS", {})
    monkeypatch.setattr(doc_goal_loop, "_APPLIED", {})
    slot = SimpleNamespace(key="doc-slot", _doc=DOC, title="Plan")
    state = SimpleNamespace(_slots={"doc-slot": slot})
    return svc, state, calls


@pytest.mark.asyncio
async def test_the_first_goal_arms_a_loop_and_runs_a_check_now(rig):
    svc, state, calls = rig
    assert await sync_goal_loop(state, DOC, MARKED) == "armed"
    assert calls == [("arm", "doc-slot", 10)]
    assert svc.fired == [svc.loops["doc-slot"].id]
    # A save that changes nothing about the goals leaves the loop alone.
    assert await sync_goal_loop(state, DOC, MARKED + "more text\n") == "unchanged"
    assert calls == [("arm", "doc-slot", 10)]
    assert len(svc.fired) == 1


@pytest.mark.asyncio
async def test_a_new_rate_rewrites_the_loop_without_a_second_arm(rig):
    svc, state, calls = rig
    await sync_goal_loop(state, DOC, MARKED)
    assert await sync_goal_loop(state, DOC, f"---\ngoal-every: 30\n---\n{MARKED}") == "updated"
    assert [c[0] for c in calls] == ["arm", "patch"]
    assert svc.loops["doc-slot"].idle_secs == 1800
    assert len(svc.fired) == 1


@pytest.mark.asyncio
async def test_the_last_goal_going_deletes_the_loop(rig):
    svc, state, calls = rig
    await sync_goal_loop(state, DOC, MARKED)
    assert await sync_goal_loop(state, DOC, "# Tasks\none\n") == "removed"
    assert svc.loops == {}
    assert await sync_goal_loop(state, DOC, "# Tasks\none\n") == "unchanged"
    # Set again: a fresh loop and a fresh first check.
    assert await sync_goal_loop(state, DOC, MARKED) == "armed"
    assert len(svc.fired) == 2


@pytest.mark.asyncio
async def test_a_paused_loop_of_ours_is_revived_and_checked(rig):
    svc, state, calls = rig
    await sync_goal_loop(state, DOC, MARKED)
    svc.loops["doc-slot"].active = False
    assert await sync_goal_loop(state, DOC, MARKED) == "updated"
    assert calls[-1] == ("patch", svc.loops["doc-slot"].id, 10, True)
    assert svc.loops["doc-slot"].active is True
    assert len(svc.fired) == 2


@pytest.mark.asyncio
async def test_a_loop_the_conductor_armed_itself_is_never_touched(rig):
    svc, state, calls = rig
    own = SimpleNamespace(
        id="W1",
        slot_key="doc-slot",
        gate=True,
        monitor=None,
        banner="",
        active=True,
        message="Read your ledger.",
        idle_secs=3600,
    )
    svc.loops["doc-slot"] = own
    assert (await sync_goal_loop(state, DOC, MARKED)).startswith("refused")
    assert await sync_goal_loop(state, DOC, "# none\n") == "unchanged"
    assert svc.loops == {"doc-slot": own} and svc.fired == [] and calls == []


@pytest.mark.asyncio
async def test_a_doc_with_no_session_or_another_kind_does_nothing(rig):
    svc, state, calls = rig
    other = doc_files.mint_doc_id("doc")
    assert await sync_goal_loop(state, other, MARKED) == "unchanged"
    assert await sync_goal_loop(state, doc_files.mint_doc_id("codebase"), MARKED) == "unchanged"
    assert calls == []


@pytest.mark.asyncio
async def test_arm_and_patch_pass_the_real_chokepoints_arguments_they_accept(monkeypatch):
    import inspect

    from kiro_crew import autonudge_authz as authz

    seen: dict[str, dict] = {}

    def checked(real, name):
        signature = inspect.signature(real)

        async def fake(**kwargs):
            signature.bind(**kwargs)
            seen[name] = kwargs
            return SimpleNamespace(id="L1"), None, 200

        return fake

    monkeypatch.setattr(
        authz, "authorize_and_add_nudge", checked(authz.authorize_and_add_nudge, "add")
    )
    monkeypatch.setattr(
        authz, "authorize_and_update_nudge", checked(authz.authorize_and_update_nudge, "update")
    )
    loop, error = await doc_goal_loop._arm(object(), object(), "doc-slot", "msg", 30)
    assert error is None and loop.id == "L1"
    assert seen["add"]["gate"] is False and seen["add"]["banner"] == GOALS_BANNER
    assert seen["add"]["idle_secs"] == 1800 and seen["add"]["replace_existing"] is False
    assert await doc_goal_loop._patch(object(), "L1", "msg", 5, True) is None
    assert seen["update"]["idle_secs"] == 300 and seen["update"]["active"] is True
    assert seen["update"]["fresh_run"] is True
