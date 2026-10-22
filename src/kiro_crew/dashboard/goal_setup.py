"""The nudge a crewmate gets right after the user adds or changes one of its goals.

Saving a goal on the Goals tab, new or edited (or applying a ``crewmate.goal`` card, which
writes through the same route) should not leave the user to work out what
milestones are or where the check-ins live. So the gateway sends the crewmate
one hidden turn in its own thread (:func:`mate_welcome.dispatch_hidden_turn`):
acknowledge the goal in a sentence, hand the research to a worker session
(``session_create`` / ``session_send``; a crewmate's chat starts no
sub-agents) and end the turn, then propose milestones when the worker's
report wakes it (:mod:`kiro_crew.dashboard.mate_thread_wakes`).
Applying that milestones card always sends a second hidden turn
(:func:`schedule_milestones_nudge`), which offers the check-ins when none is on. Each of those is a card the user
decides; the crewmate's own goal cards are admitted outside a turn the user sent
(``handlers.guide.REQ_OWN_GOAL_TURN``).

The nudge runs only when the goal really changed: an Undo putting an older
goal back and a small edit (a typo) send nothing. Removing a goal on the Goals
tab sends its own short nudge (:func:`schedule_removed_nudge`): acknowledge it,
stop work that only served it, and propose dropping or relinking milestones
left serving no goal. The setup nudge names the goal
that changed by its number and lists every goal. When the crewmate is
busy the nudge waits for its thread to go idle, for a bounded time, and one
crewmate holds at most one waiting nudge: a newer save replaces it.

Every nudge names the ``kirocrew-mate`` card tools only when the crewmate's
sessions load them (:func:`kiro_crew.member_mate_tools.member_has_mate_tools`);
without them it writes the milestones or check-ins in the chat and points the
user at the Goals tab.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew import crewmate_prompts, member_goals
from kiro_crew import members as members_mod

logger = logging.getLogger(__name__)

#: Above this similarity a new goal reads as an edit of the old one, not a new goal.
SMALL_EDIT_RATIO = 0.9
#: How long a nudge waits for a busy thread before it is dropped.
WAIT_SECS = 600.0
POLL_SECS = 3.0


def changed_enough(before: str, after: str) -> bool:
    """Whether a save from *before* to *after* is a new goal worth a nudge."""
    new = " ".join(after.split()).casefold()
    if not new:
        return False
    old = " ".join(before.split()).casefold()
    if not old:
        return True
    return difflib.SequenceMatcher(None, old, new).ratio() < SMALL_EDIT_RATIO


def kickoff_prompt(
    rec: dict[str, Any],
    goal_id: str = "",
    *,
    has_mate_tools: bool = True,
    dashboard_line: str = "",
) -> str | None:
    """The hidden turn's text, built from the goal record as stored.

    Names the goal *goal_id* that was just added or changed (``None`` when it is
    gone by the time the turn would run) and shows the whole list. Without the
    ``kirocrew-mate`` tools (*has_mate_tools*) the milestones are written in
    the chat for the user to add on the Goals tab. *dashboard_line* is
    ``goal_reminder.dashboard_line`` (the build line on an empty tab, or the one-time
    page offer on a template page), added last.
    """
    numbers = member_goals.goal_numbers(rec)
    if goal_id not in numbers:
        return None
    goal = rec["goals"][numbers[goal_id] - 1]
    n = numbers[goal_id]
    has_krs = bool(rec.get("key_results"))
    listing = [
        f"{i}. {member_goals.scrub_text(g['text'])}" for i, g in enumerate(rec["goals"], start=1)
    ]
    T = crewmate_prompts.text  # noqa: N806 - a short local alias for the reads below
    step3 = T(
        "goal_setup.kickoff_step3_tools" if has_mate_tools else "goal_setup.kickoff_step3_no_tools"
    )
    if has_krs:
        step3 += T("goal_setup.kickoff_has_milestones")
    lines = [
        crewmate_prompts.fill(
            "goal_setup.kickoff_head",
            n=n,
            goal_id=goal_id,
            goal=member_goals.scrub_text(goal["text"]),
            listing="\n".join(listing),
        ),
        crewmate_prompts.fill("goal_setup.kickoff_step2", n=n),
        step3 + T("goal_setup.kickoff_unmeasured"),
        T(
            "goal_setup.kickoff_step4_tools"
            if has_mate_tools
            else "goal_setup.kickoff_step4_no_tools"
        ),
        T("goal_setup.kickoff_small_set"),
        T("goal_setup.one_thing"),
    ]
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _tasks(state: Any) -> dict[str, asyncio.Task[Any]]:
    tasks = getattr(state, "_goal_setup_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        state._goal_setup_tasks = tasks
    return tasks


#: Builds a nudge from the goal record and whether the crewmate has the mate tools.
Build = Callable[[dict[str, Any], bool], str | None]


async def _wait_and_send(state: Any, slug: str, member: str, build: Build) -> str:
    from kiro_crew.dashboard.goal_reminder import _idle_thread
    from kiro_crew.dashboard.mate_welcome import dispatch_hidden_turn

    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECS
    while True:
        binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
        slot_key = binding.get("slot_key", "") if binding else ""
        slot, skip = _idle_thread(state, slot_key, member)
        if slot is not None:
            try:
                rec = await asyncio.to_thread(member_goals.read_goals, slug, member)
            except member_goals.GoalsUnreadable:
                return "skipped: goal record unreadable"
            from kiro_crew.member_mate_tools import member_has_mate_tools

            has_tools = await asyncio.to_thread(member_has_mate_tools, member)
            # Off the loop: the goal-setup build reads the crewmate's dashboard binding.
            prompt = await asyncio.to_thread(build, rec, has_tools)
            if prompt is None:
                return "skipped: nothing left to talk about"
            # The read ran off the loop: a turn may have started meanwhile.
            again, skip = _idle_thread(state, slot_key, member)
            if again is slot:
                dispatch_hidden_turn(state, slot, prompt, human_seen=True)
                return "started"
        if skip == "skipped: the mate's thread is not open" or loop.time() >= deadline:
            return skip or "skipped: timed out"
        await asyncio.sleep(POLL_SECS)


def _goal_prompt(goal_id: str, slug: str = "") -> Build:
    def build(rec: dict[str, Any], tools: bool) -> str | None:
        from kiro_crew.dashboard.goal_reminder import dashboard_line

        return kickoff_prompt(
            rec,
            goal_id,
            has_mate_tools=tools,
            dashboard_line=dashboard_line(slug, rec, has_mate_tools=tools),
        )

    return build


#: The Heartbeat in the check-ins offer. It is off by default and only a check-ins
#: card or the Goals tab turns it on, so the offer names it and what each wake costs.
_HEARTBEAT_OFFER = (
    "and what the Heartbeat does: a run every so often that keeps the work moving "
    "while they are away, with this chat staying quiet and your Dashboard tab "
    "showing what it did, each run using credits."
)


def milestones_prompt(
    rec: dict[str, Any], *, has_mate_tools: bool = True, dashboard_line: str = ""
) -> str | None:
    """The hidden turn after the user confirms the crewmate's milestones card.

    Without the ``kirocrew-mate`` tools (*has_mate_tools*) the check-ins are
    described and left to the user's Goals tab instead of ``set_check_ins``.
    *dashboard_line* is ``goal_reminder.dashboard_line`` (the build line or the
    one-time page offer), added last: confirming the milestones is the moment a crewmate first knows what
    its page would be about.
    """
    krs = rec.get("key_results") or []
    if not krs:
        return None
    sched = rec.get("schedules") or {}
    any_on = any(sched.get(c) for c in member_goals.CADENCES)
    lines = [crewmate_prompts.text("goal_setup.milestones_head"), ""]
    for kr in krs:
        text = member_goals.scrub_text(str(kr.get("text") or ""))
        target = member_goals.scrub_text(str(kr.get("target") or ""))
        serves = member_goals.serves_note(rec, kr)
        lines.append(f"- {text} (aiming for: {target}){serves}" if target else f"- {text}{serves}")
    goals = [{**g, "text": member_goals.scrub_text(g["text"])} for g in rec.get("goals") or []]
    if goals:
        lines.extend(["", *member_goals.goal_lines({"goals": goals})])
    lines.append("")
    if any_on:
        lines.append(crewmate_prompts.text("goal_setup.milestones_running"))
    elif has_mate_tools:
        lines.append(
            "Reply in the user's language: acknowledge them in a sentence, then offer "
            "check-ins with set_check_ins. Say in one plain sentence each what the Daily "
            "Goal Reminder, the Weekly report and the Monthly review would do for them, "
            + _HEARTBEAT_OFFER
            + " Propose the ones that fit this goal, and let the card decide."
        )
    else:
        lines.append(
            "Reply in the user's language: acknowledge them in a sentence, then offer "
            "check-ins. Say in one plain sentence each what the Daily Goal Reminder, the "
            "Weekly report and the Monthly review would do for them, "
            + _HEARTBEAT_OFFER
            + " Suggest the ones that fit this goal, and tell the user they turn them on "
            "on your Goals tab (the Heartbeat on your Schedules tab)."
        )
    if dashboard_line:
        lines.extend(["", dashboard_line])
    return "\n".join(lines)


def _schedule(
    state: Any,
    key: str,
    slug: str,
    member: str,
    build: Build,
    what: str,
    on_dropped: "Callable[[str], None] | None" = None,
    on_started: "Callable[[], None] | None" = None,
) -> None:
    """Send *build*'s turn to *member* once its thread is idle, one waiting per *key*.

    *on_dropped*, when given, runs off the loop with the outcome when the turn was
    never started (the wait timed out, the thread closed, the read failed), so a
    caller that promised a wake can take that promise back. *on_started*, when
    given, runs off the loop once the turn was started, so a caller that kept a
    durable mark of the promise can clear it.
    """
    if state is None:
        return
    tasks = _tasks(state)
    old = tasks.get(key)
    if old is not None and not old.done():
        old.cancel()

    async def run() -> None:
        try:
            outcome = await _wait_and_send(state, slug, member, build)
            logger.info("%s nudge for %s: %s", what, slug, outcome)
            if outcome == "started":
                if on_started is not None:
                    await asyncio.to_thread(on_started)
            elif on_dropped is not None:
                await asyncio.to_thread(on_dropped, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("%s nudge for %s failed", what, slug, exc_info=True)
        finally:
            if tasks.get(key) is task:
                tasks.pop(key, None)

    task = asyncio.get_running_loop().create_task(run())
    tasks[key] = task


def schedule_nudge(state: Any, slug: str, member: str, goal_id: str) -> None:
    """Send *member* the goal-setup nudge for *goal_id* once its thread is idle.

    One waiting nudge per crewmate: a newer save, of this goal or another,
    replaces it.
    """
    _schedule(state, slug, slug, member, _goal_prompt(goal_id, slug), "goal setup")


def schedule_milestones_nudge(state: Any, slug: str, member: str) -> None:
    """Tell *member* the user confirmed its milestones, once its thread is idle."""
    _schedule(
        state,
        f"{slug}:milestones",
        slug,
        member,
        _milestones_build(slug),
        "milestones confirmed",
    )


def _milestones_build(slug: str) -> Build:
    def build(rec: dict[str, Any], tools: bool) -> str | None:
        from kiro_crew.dashboard.goal_reminder import dashboard_line

        return milestones_prompt(
            rec,
            has_mate_tools=tools,
            dashboard_line=dashboard_line(slug, rec, has_mate_tools=tools),
        )

    return build


def capabilities_prompt(added: str) -> str:
    """The hidden turn after the user applies the crewmate's capabilities card.

    Without it the grant sat unused: a card press starts no turn, and a worker
    that was already running cannot see what was added, so nothing moved until
    the user typed. *added* is the card's title, which names what was added.
    """
    return crewmate_prompts.fill(
        "goal_setup.capabilities_added", added=member_goals.scrub_text(added)
    )


def tool_access_prompt(raised: bool = True) -> str:
    """The hidden turn after the user turns the crewmate's preview on (the opt-in card).

    *raised* when the request that raised the card is now up as its own
    capabilities card; otherwise the crewmate is asked to look and ask again.
    """
    key = "goal_setup.tool_access_on" if raised else "goal_setup.tool_access_on_ask"
    return crewmate_prompts.text(key)


def schedule_tool_access_nudge(
    state: Any, slot_key: str, *, app: Any = None, session_key: str = "", card_member: str
) -> None:
    """Raise the held request, reload the crewmate's conductor, then wake it.

    The opt-in card (``kiro_crew.mate_tool_access``) turned on this crewmate's
    Dynamic capabilities. First the ``request_capabilities`` call that raised it
    becomes its own capabilities card in the same thread
    (``member_self.continue_after_opt_in``), so the crewmate does not ask again.
    Its conductor's context now names ``find_capabilities``; a running conductor
    keeps the session-start context it began with, so the SAME session is reloaded
    (conversation kept, ``crewmate_worker_reload.reload_if_idle``) and then told
    what happened.
    Only a crewmate's own pinned thread is told, and only for a card that turned
    on THAT crewmate's flag (*card_member*, the card's ``params.name``): a card for
    another crewmate raised in this thread wakes nobody here.
    """
    from kiro_crew.dashboard.crewmate_worker_reload import reload_if_idle
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread

    slot = state.get_slot(slot_key) if state is not None else None
    if slot is None or not is_crewmate_thread(slot):
        return
    member = slot.agent
    if not member or card_member != member:
        return

    async def start() -> None:
        raised = False
        if app is not None and session_key:
            from kiro_crew.dashboard.handlers.member_self import continue_after_opt_in

            try:
                why = await continue_after_opt_in(app, slot_key, session_key, member)
            except Exception:  # noqa: BLE001 - the wake below asks the crewmate again
                logger.warning("held capabilities request of %s failed", member, exc_info=True)
                why = "failed"
            raised = not why
            if why:
                logger.info("held capabilities request of %s not raised: %s", member, why)
        await reload_if_idle(state, slot)
        slug = await asyncio.to_thread(members_mod.member_slug, member)
        _schedule(
            state,
            f"{slug}:tool-access",
            slug,
            member,
            lambda _rec, _tools: tool_access_prompt(raised),
            "tool access on",
        )

    task = asyncio.get_running_loop().create_task(start())
    _tasks(state)[f"{slot_key}:tool-access-start"] = task
    task.add_done_callback(lambda _t: _tasks(state).pop(f"{slot_key}:tool-access-start", None))


def schedule_capabilities_nudge(state: Any, slot_key: str, added: str, *, card_member: str) -> None:
    """Tell the crewmate of thread *slot_key* that its capabilities card was applied.

    Once its thread is idle, through the same wait as the other goal nudges.
    Only a crewmate's own pinned thread is told, and only for a card that changed
    THAT crewmate's worker (*card_member*, the card's ``params.member``): a card
    for another crewmate raised in this thread wakes nobody here.
    """
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread

    slot = state.get_slot(slot_key) if state is not None else None
    if slot is None or not is_crewmate_thread(slot):
        return
    member = slot.agent
    if not member or card_member != member:
        return

    async def start() -> None:
        # The slug read loads the config, so it runs off the loop.
        slug = await asyncio.to_thread(members_mod.member_slug, member)
        _schedule(
            state,
            f"{slug}:capabilities",
            slug,
            member,
            lambda _rec, _tools: capabilities_prompt(added),
            "capabilities added",
        )

    task = asyncio.get_running_loop().create_task(start())
    _tasks(state)[f"{slot_key}:capabilities-start"] = task
    task.add_done_callback(lambda _t: _tasks(state).pop(f"{slot_key}:capabilities-start", None))


def removed_prompt(rec: dict[str, Any], goal_id: str, *, has_mate_tools: bool = True) -> str | None:
    """The hidden turn after the user removes goal *goal_id* on the Goals tab.

    ``None`` unless that removal is the record's newest one (an Undo put it
    back, or another goal was removed since). Without the ``kirocrew-mate``
    tools (*has_mate_tools*) the milestone change is suggested in the chat for
    the user to make on the Goals tab.
    """
    removed = rec.get("removed_goal")
    if not isinstance(removed, dict) or removed.get("goal", {}).get("id") != goal_id:
        return None
    text = member_goals.scrub_text(str(removed["goal"].get("text") or ""))
    served = set(removed.get("key_result_ids") or [])
    lines = [crewmate_prompts.text("goal_setup.removed_head"), "", text, ""]
    goals = [{**g, "text": member_goals.scrub_text(g["text"])} for g in rec.get("goals") or []]
    if goals:
        lines.extend(["Goals left:", *member_goals.goal_lines({"goals": goals})[1:], ""])
    else:
        lines.extend([crewmate_prompts.text("goal_setup.removed_none_left"), ""])
    orphans = [
        member_goals.scrub_text(str(kr.get("text") or ""))
        for kr in rec.get("key_results") or []
        if kr.get("id") in served and not kr.get("goal_ids")
    ]
    if orphans:
        lines.extend(
            [crewmate_prompts.text("goal_setup.removed_orphans"), *(f"- {o}" for o in orphans), ""]
        )
        follow_up = crewmate_prompts.text(
            "goal_setup.removed_relink_tools"
            if has_mate_tools
            else "goal_setup.removed_relink_no_tools"
        )
    else:
        follow_up = crewmate_prompts.text("goal_setup.removed_nothing_else")
    lines.append(crewmate_prompts.text("goal_setup.removed_reply") + follow_up)
    return "\n".join(lines)


def schedule_removed_nudge(state: Any, slug: str, member: str, goal_id: str) -> None:
    """Tell *member* the user removed goal *goal_id*, once its thread is idle.

    It shares the goal-setup slot: a later save replaces it, and it replaces a
    nudge still waiting for the goal the user just removed.
    """
    _schedule(
        state,
        slug,
        slug,
        member,
        lambda rec, tools: removed_prompt(rec, goal_id, has_mate_tools=tools),
        "goal removed",
    )
