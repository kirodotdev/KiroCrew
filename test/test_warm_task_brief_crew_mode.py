"""The warm task brief on a Crew Mode chat: a dashboard chat running a conductor.

The trigger, claim and spend rules are the crewmate's own (``test_warm_task_brief.py``);
these tests pin that a Crew Mode root is eligible under exactly those rules, that its
workers and ordinary chats are not, and that the prompt walks the conductor's ledger.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_warm_task_brief import _FULL_BRIEF, _brief_kinds, _focus, _seen, _thread

from kiro_crew import crew_mode
from kiro_crew.dashboard import slot_retention, warm_task_brief


def _crew_mode_chat(monkeypatch, *, user_ago: float | None, goal: bool = True):
    """A Crew Mode root (a chat on the conductor) with a goal and two work items."""
    state, slot, log = _thread(monkeypatch, user_ago=user_ago)
    slot.mode = "chat"
    slot.agent = crew_mode.DEFAULT_CREW_MODE_AGENT
    slot.agent_kind = "template"
    slot.memory_mode = "persistent"
    record = (
        SimpleNamespace(
            slot_key=slot.key,
            goal="Ship the welcome in Crew Mode",
            round=1,
            depth=0,
            parent_item="",
            generation=1,
            goal_version=0,
            created_at="",
            created_total=2,
            item_cap=20,
            concurrency_cap=6,
        )
        if goal
        else None
    )
    items = [
        SimpleNamespace(
            item_id=f"it_{n:08x}",
            title=title,
            state=item_state,
            verdict="",
            decision="",
            worker_session_key=f"chat-w{n}",
            status=status,
            reason="needs_human" if status == "question" else "",
            summary=summary,
            artifacts={},
            last_report_at="",
            created_at="",
            closed_at="",
            decided_at="",
        )
        for n, (title, item_state, status, summary) in enumerate(
            [
                ("Fix the login bug", "accepted", "done", "PR 1 merged"),
                ("Add the panel", "open", "question", "Which layout?"),
            ]
        )
    ]
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (record, items))
    monkeypatch.setattr(warm_task_brief, "read_state", lambda key: {})
    return state, slot, log


async def _hide_for(state, conn: int, slot_key: str, away: float) -> None:
    """Show *slot_key*, then hide the page and stay away *away* seconds."""
    slot_retention.note_foreground(state, conn, slot_key)
    warm_task_brief.handle_focus_frame(state, conn, None, hidden=True)
    slot_retention.note_foreground(state, conn, None)
    _seen(state, slot_key, away)


def test_a_crew_mode_root_is_a_brief_thread(monkeypatch):
    _, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3600)
    assert warm_task_brief.is_brief_thread(slot)


def test_a_crewmate_thread_stays_a_brief_thread(monkeypatch):
    _, slot, _ = _thread(monkeypatch, user_ago=3600)
    assert warm_task_brief.is_brief_thread(slot)


@pytest.mark.asyncio
async def test_crew_mode_return_after_45_minutes_briefs_once(monkeypatch):
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"One item is done, one waits on you.","needs_you":["Pick a layout"]}'

    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=2 * 3600)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    await _hide_for(state, 1, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == _FULL_BRIEF
    # One background call per qualifying return, the crewmate's cost.
    assert len(prompts) == 1
    assert "You are the conductor in this chat." in prompts[0]
    assert "which wait on the owner" in prompts[0]
    assert "Ship the welcome in Crew Mode" in prompts[0]
    assert "Which layout?" in prompts[0]
    result = slot.rows[-1]
    # RC7: a board with no recorded goal counts every item it ever had, so the
    # brief shows no count rather than one that reads like today's progress.
    assert "progress" not in result["meta"]["brief"]
    assert '"goal_progress": null' in prompts[0]
    # The gateway lists what waits on the owner from the ledger, not the model.
    assert result["meta"]["brief"]["waiting"] == [
        {"title": "Add the panel", "ask": "Which layout?"}
    ]
    assert result["meta"]["brief"]["waiting_total"] == 1

    # Leave and come back again with nothing changed: the claim holds, no spend.
    await _hide_for(state, 1, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert len(prompts) == 1


@pytest.mark.asyncio
async def test_crew_mode_short_away_gives_no_brief(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=2 * 3600)
    await _hide_for(state, 1, slot.key, 10 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_crew_mode_visible_idle_page_gives_no_brief(monkeypatch):
    state, slot, log = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    _seen(state, slot.key, 2 * 3600)
    slot_retention.note_foreground(state, 1, slot.key)
    await _focus(state, 1, slot.key)
    await _focus(state, 2, slot.key)
    assert _brief_kinds(slot) == []
    assert log.reads == 0


@pytest.mark.asyncio
async def test_crew_mode_reconnect_announce_after_restart_gives_no_brief(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    await _focus(state, 1, slot.key, announce=True)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_crew_mode_without_a_goal_or_work_gives_no_brief(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=2 * 3600, goal=False)
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (None, []))
    await _hide_for(state, 1, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


def test_a_crew_mode_worker_never_starts_a_brief(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=2 * 3600)
    slot._created_by = "chat-root"
    _seen(state, slot.key, 45 * 60)
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    assert state._warm_task_brief_tasks == {}


def test_an_ordinary_chat_on_a_normal_agent_never_starts_a_brief(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=2 * 3600)
    slot.agent = "kirocrew"
    _seen(state, slot.key, 45 * 60)
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    assert state._warm_task_brief_tasks == {}


@pytest.mark.asyncio
async def test_crewmate_return_keeps_the_crewmate_voice(monkeypatch):
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"Still fixing the test."}'

    state, slot, _ = _thread(monkeypatch, user_ago=2 * 3600)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == _FULL_BRIEF
    assert len(prompts) == 1
    assert "You are the crewmate in this chat." in prompts[0]
    assert "which wait on the owner" not in prompts[0]


@pytest.mark.asyncio
async def test_crew_mode_pane_switch_while_still_visible_gives_no_brief(monkeypatch):
    # Split view: focus moves to another pane; the Crew Mode pane stays on screen.
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    slot_retention.note_foreground(state, 1, slot.key)
    await _focus(state, 1, "chat-other")
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_crew_mode_socket_loss_then_return_gives_no_brief(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    slot_retention.note_foreground(state, 1, slot.key)
    warm_task_brief.note_view_closed(state, 1)
    slot_retention.note_foreground(state, 1, None)
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 2, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_crew_mode_return_after_restart_gives_no_brief(monkeypatch):
    # No on-screen record and no hide this process saw: the durable times alone
    # brief a crewmate, never a Crew Mode chat.
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_crew_mode_hide_mark_is_spent_by_the_return(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    await _hide_for(state, 1, slot.key, 10 * 60)
    await _focus(state, 1, slot.key)  # a short return spends the hide
    await _focus(state, 1, "chat-other")
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_crewmate_pane_switch_still_briefs(monkeypatch):
    # The crewmate rule is unchanged: leaving its thread for another is away time.
    state, slot, _ = _thread(monkeypatch, user_ago=3 * 3600)
    slot_retention.note_foreground(state, 1, slot.key)
    await _focus(state, 1, "thread-y")
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == _FULL_BRIEF


@pytest.mark.asyncio
async def test_crew_mode_hide_mark_is_spent_by_a_reveal_naming_another_slot(monkeypatch):
    # Split view: the hide left pane B, but the reveal names A. B's pane then
    # stays visible for 45 minutes before the owner clicks it.
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    await _hide_for(state, 1, slot.key, 0)
    await _focus(state, 1, "chat-a")
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_crew_mode_hide_in_one_window_while_another_shows_it_gives_no_brief(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    slot_retention.note_foreground(state, 2, slot.key)  # window 2 shows it too
    await _hide_for(state, 1, slot.key, 0)  # window 1 hides; A is still on screen
    await _focus(state, 2, "chat-other")  # window 2 switches panes, A stays visible
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)  # window 1 comes back to A
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_crew_mode_another_window_opening_the_chat_spends_the_hide(monkeypatch):
    state, slot, _ = _crew_mode_chat(monkeypatch, user_ago=3 * 3600)
    await _hide_for(state, 1, slot.key, 0)  # window 1 hides on A
    await _focus(state, 2, slot.key)  # window 2 opens A
    await _focus(state, 2, "chat-other")  # window 2 switches panes, A stays visible
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)  # window 1 comes back to A
    assert _brief_kinds(slot) == []


def test_hidden_leave_marks_are_bounded():
    state = SimpleNamespace(_warm_task_brief_hidden_left={})
    for n in range(warm_task_brief._MAX_LAST_SEEN + 5):
        warm_task_brief._mark_hidden_leave(state, n, f"chat-{n}")
    assert len(state._warm_task_brief_hidden_left) == warm_task_brief._MAX_LAST_SEEN
