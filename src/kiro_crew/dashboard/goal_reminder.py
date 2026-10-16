"""A crewmate's check-ins: Daily Goal Reminder, Weekly report, Monthly review, Heartbeat.

The Goals tab's four switches (``PUT /api/members/{slug}/goals/schedules``) are
stored in the goal record (:mod:`kiro_crew.member_goals`), and each switch keeps
exactly ONE gateway-owned schedule in step with it, through the cron store's
installer key (``managed_by``, ``goal-<cadence>:<slug>``), so no switch can ever
have two schedules and none touches another's:

* on -> that cadence's old schedule (if any) is removed and a fresh one created
  at the shared time of day: daily ``M H * * *``, weekly ``M H * * <weekday>``,
  monthly ``M H <day> * *``, in the user's timezone; the heartbeat runs on its
  interval instead (``*/30 * * * *``, ``0 * * * *``, ``0 */2 * * *``,
  ``0 */4 * * *``, :func:`cron_expr_for`);
* off -> that cadence's schedule is removed.

When a schedule fires, the gateway's cron callback hands the job here
(:func:`fire`) instead of running it as an ordinary cron turn: the prompt is
built from the CURRENT goal record and dispatched as one gateway-authored turn
in the crewmate's pinned thread (:func:`mate_welcome.dispatch_hidden_turn`), so
the user sees only the crewmate's reply. That turn is not one the user sent, so
``update_self`` and most cards refuse inside it; the goal tools (readings,
``save_goal_report``) are admitted there on purpose
(``handlers.member_goals``), and so are the crewmate's own goal, milestones and
check-ins cards (``set_goal``, ``set_key_results``, ``set_check_ins``), which
any turn in its own thread may raise (``handlers.guide.REQ_OWN_GOAL_TURN``).
Nothing changes until the user presses the card.

The weekly and monthly prompts carry the tasks the crewmate finished in the
period, read from its work ledger (the same items the Crew board shows,
:func:`finished_tasks`). The weekly and monthly turns end with ``save_goal_report``: the gateway saves the
report as an artifact under ``Crewmates/<name>/Reports`` (:func:`save_report`),
remembers it on the goal record, and sends one notification linking it; the
crewmate's reply in the thread is one short line, never the report itself.

The heartbeat (RFC "Crewmate heartbeat") is the one check-in that does the work
rather than reporting on it: its turn looks at the goals, milestones and open
work-ledger items, moves forward what it can, records the result where the
user looks (the work ledger, the Dashboard tab, milestone readings) and ends
with ``nothing_to_do`` unless the user would lose something by not hearing now
(:func:`heartbeat_prompt`). That quiet end is admitted because a hidden turn is
opened by the gateway, not by a person (``session_directive_apply._nothing_to_do``
refuses it only on a user-facing or channel turn), and the reply text of a
heartbeat turn that ends that way is folded into its steps rather than shown
as a chat message (``dispatch_hidden_turn``, settled by
:mod:`kiro_crew.dashboard.heartbeat_quiet_end`). Long work goes to a worker
session when the crewmate's member backend mounts the ``session_*`` tools
(:func:`member_has_session_tools`); otherwise it is done in small steps across
wakes. Either way the next wake is the patrol. Once every milestone is met the
wake carries the gateway's own line asking for the next set first
(:func:`milestones_line`). A heartbeat that fires while the gateway is stopping
starts no turn (:func:`gateway_stopping`). Only the owner's route and an
applied card write the switch, so the crewmate cannot turn its own heartbeat on.

A check-in that fires with the thread closed, a turn already running, its switch
off or no goal left does nothing that time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from kiro_crew import member_goals
from kiro_crew import members as members_mod

logger = logging.getLogger(__name__)

#: Installer key prefix per cadence; the rest is the member slug.
MANAGED_PREFIXES = {c: f"goal-{c}:" for c in member_goals.CADENCES}

#: Who the cron store records as removing a check-in schedule.
_ACTOR = "dashboard"
_SOURCE = "goal_checkins"

#: Where reports are filed, under the crewmate's display name.
REPORTS_FOLDER = "Crewmates/{name}/Reports"

#: How far back the weekly report and the monthly review look at readings.
_WINDOW_SECS = {
    member_goals.CADENCE_WEEKLY: 7 * 24 * 3600,
    member_goals.CADENCE_MONTHLY: 31 * 24 * 3600,
}


def managed_key(slug: str, cadence: str = member_goals.CADENCE_DAILY) -> str:
    return MANAGED_PREFIXES[cadence] + members_mod.validate_slug(slug)


def cadence_of(job: Any) -> str | None:
    key = getattr(job, "managed_by", "")
    if not isinstance(key, str):
        return None
    for cadence, prefix in MANAGED_PREFIXES.items():
        if key.startswith(prefix):
            return cadence
    return None


def is_reminder_job(job: Any) -> bool:
    """Whether *job* is one of a crewmate's goal check-ins (daily, weekly, monthly, heartbeat).

    The one predicate every list that leaves them out reads: the Goals tab's
    switches are their only control, so ``GET /api/crons`` (the Schedule page and
    a crewmate's Schedules tab, which filters that same list) omits them while
    the cron store keeps running them.
    """
    return cadence_of(job) is not None


#: The heartbeat's cron expression per interval (minutes), on the clock's own
#: marks: a 30-minute heartbeat runs at :00 and :30.
_HEARTBEAT_EXPRS = {
    30: "*/30 * * * *",
    60: "0 * * * *",
    120: "0 */2 * * *",
    240: "0 */4 * * *",
}


def heartbeat_interval(schedules: dict[str, Any]) -> int:
    """The record's heartbeat interval in minutes, the default when it holds none."""
    value = schedules.get("interval")
    if isinstance(value, int) and member_goals.is_heartbeat_interval(value):
        return value
    return member_goals.DEFAULT_HEARTBEAT_INTERVAL


def cron_expr_for(cadence: str, schedules: dict[str, Any]) -> str:
    """The cron expression for *cadence* at the record's shared time (or interval)."""
    if cadence == member_goals.CADENCE_HEARTBEAT:
        return _HEARTBEAT_EXPRS[heartbeat_interval(schedules)]
    hour, minute = (int(x) for x in str(schedules["time"]).split(":"))
    if cadence == member_goals.CADENCE_WEEKLY:
        return f"{minute} {hour} * * {int(schedules['weekday'])}"
    if cadence == member_goals.CADENCE_MONTHLY:
        return f"{minute} {hour} {int(schedules['month_day'])} * *"
    return f"{minute} {hour} * * *"


async def reminder_jobs(crons: Any, slug: str, cadence: str | None = None) -> list[Any]:
    keys = (
        {managed_key(slug, cadence)}
        if cadence
        else {managed_key(slug, c) for c in member_goals.CADENCES}
    )
    return [j for j in await crons.list_jobs_async(include_disabled=True) if j.managed_by in keys]


_NAMES = {
    member_goals.CADENCE_DAILY: "Daily goal reminder",
    member_goals.CADENCE_WEEKLY: "Weekly report",
    member_goals.CADENCE_MONTHLY: "Monthly review",
    member_goals.CADENCE_HEARTBEAT: "Heartbeat",
}


async def sync_schedule(crons: Any, slug: str, label: str, schedules: dict[str, Any]) -> None:
    """Make the store hold exactly the schedules *schedules* asks for, one per switch.

    A cadence whose job already matches (one job, enabled, same cron expression,
    timezone and name) is left as it is, run history included: saving one
    switch must not wipe the history of the others. A cadence that changed has
    its old schedule removed first, then a fresh one added with a new id when
    its switch is on: the cron store's installer contract replaces a managed
    job that way (no replace-in-place), so a run in flight only removes a dead
    one.
    """
    from kiro_crew.cron_service.schedule import get_local_tz

    tz = schedules.get("timezone") or get_local_tz()[0]
    for cadence in member_goals.CADENCES:
        jobs = await reminder_jobs(crons, slug, cadence)
        wanted = bool(schedules.get(cadence))
        expr = cron_expr_for(cadence, schedules) if wanted else None
        name = f"{_NAMES[cadence]}: {label}"[:200]
        if (
            wanted
            and len(jobs) == 1
            and jobs[0].enabled
            and jobs[0].schedule.kind == "cron"
            and jobs[0].schedule.cron_expr == expr
            and jobs[0].timezone == tz
            and jobs[0].name == name
        ):
            continue
        for job in jobs:
            await crons.remove_job_async(job.id, actor=_ACTOR, source=_SOURCE)
        if not wanted:
            continue
        await crons.add_managed_job_async(
            managed_key(slug, cadence),
            name=name,
            message=f"{_NAMES[cadence]} for {label}. It runs in the crewmate's own chat.",
            cron_expr=cron_expr_for(cadence, schedules),
            timezone=tz,
            silent=True,
            hide_in_chat=True,
            persistent_session=False,
            minimal_context=True,
        )


_PREAMBLE = (
    "Kiro Crew sends this; the user did not type it and sees only your reply, so "
    "keep this note itself out of what you write. Write in the language of the "
    "dashboard's [UI LANGUAGE] when it is shown, else English.\n\n"
)


def _movement_lines(rec: dict[str, Any], cadence: str) -> str:
    since = time.time() - _WINDOW_SECS[cadence]
    lines = []
    for m in member_goals.key_result_movement(rec, since=since):
        start = m["start"] if m["start"] is not None else "not read"
        now = m["now"] if m["now"] is not None else "not read"
        lines.append(
            f"- {m['text']}: {start} -> {now} (target {m['target']}, "
            f"{m['readings_in_window']} readings in this period)"
        )
    return "\n".join(lines) or "(no key results yet)"


#: How many finished tasks a report prompt lists, and how much of each summary.
_MAX_FINISHED_TASKS = 30
_TASK_SUMMARY_MAX_CHARS = 300


def finished_tasks(slot_key: str, cadence: str) -> list[dict[str, str]]:
    """The work-ledger tasks the thread *slot_key* closed in *cadence*'s window.

    Read through the Crew board's own reader
    (:func:`~kiro_crew.dashboard.handlers.work_ledger_board.closed_items`, the
    slot's ledger key), newest last, each ``{title, state, summary}`` and
    scrubbed like every string the crewmate wrote. A thread that owns no ledger
    has finished nothing here. Blocking file IO.
    """
    from kiro_crew.dashboard.handlers import work_ledger_board

    since = datetime.now(timezone.utc) - timedelta(seconds=_WINDOW_SECS[cadence])
    done: list[tuple[datetime, dict[str, str]]] = []
    for row in work_ledger_board.closed_items(slot_key):
        closed = row["closed_at"]
        if closed <= since:
            continue
        done.append(
            (
                closed,
                {
                    "title": member_goals.scrub_text(row["title"]),
                    "state": row["state"],
                    "summary": member_goals.scrub_text(row["summary"])[:_TASK_SUMMARY_MAX_CHARS],
                },
            )
        )
    done.sort(key=lambda pair: pair[0])
    return [task for _closed, task in done][-_MAX_FINISHED_TASKS:]


def _finished_lines(tasks: list[dict[str, str]]) -> str:
    lines = []
    for task in tasks:
        line = f"- [{task['state']}] {task['title']}"
        if task["summary"]:
            line += f": {task['summary']}"
        lines.append(line)
    return "\n".join(lines) or (
        "(none on your task list in this period; summarise what you finished from "
        "your own work log, and say in the report that you did)"
    )


def reminder_prompt(
    rec: dict[str, Any],
    cadence: str = member_goals.CADENCE_DAILY,
    *,
    finished: list[dict[str, str]] | None = None,
) -> str:
    """The hidden turn's text for *cadence*, built from the current goal record.

    *finished* is :func:`finished_tasks` for the weekly and monthly prompts.
    """
    block = member_goals.progress_block(rec)
    done = _finished_lines(finished or [])
    if cadence == member_goals.CADENCE_DAILY:
        return (
            "[Daily goal reminder] " + _PREAMBLE + f"{block}\n\n"
            "Take a fresh reading of each key result and record it with "
            "key_result_progress (on the kirocrew-mate server); a reading can be a "
            "number or a short note. Lead your message with those readings and say "
            "briefly where things stand toward each goal, then continue "
            "with your next task. Ask the user something only when you are blocked."
        )
    if cadence == member_goals.CADENCE_WEEKLY:
        return (
            "[Weekly report] "
            + _PREAMBLE
            + f"{block}\n\nHow each key result moved this week, from its readings:\n"
            + _movement_lines(rec, cadence)
            + "\n\nTasks you finished this week, from your work ledger:\n"
            + done
            + "\n\nWrite this week's report for the user in Markdown: how each key "
            "result moved, the tasks you finished, what is stuck and why, "
            "and the plan for next week. Base the numbers on the readings above, and "
            "take a fresh reading first when one is stale. Save it with "
            'save_goal_report (kind "weekly"), then reply in the thread with one short '
            "line pointing at it; the report itself stays out of the chat."
        )
    return (
        "[Monthly review] "
        + _PREAMBLE
        + f"{block}\n\nHow each key result moved this month, from its readings:\n"
        + _movement_lines(rec, cadence)
        + "\n\nTasks you finished this month, from your work ledger:\n"
        + done
        + "\n\nReview whether your goals and their key results still fit the "
        "work you are actually doing. Write the review in Markdown: what the month "
        "showed, whether the direction still holds, and what you would change. Save "
        'it with save_goal_report (kind "monthly"), then reply in the thread with one '
        "short line pointing at it. When the key results or a goal no longer fit the "
        "work, propose the replacement as a card in this turn (set_key_results for the "
        "whole set, set_goal with that goal's id for a goal) with the reason drawn from "
        "the review; the user decides with the card's button. When they still fit, "
        "leave them as they "
        "are and propose nothing."
    )


#: How a heartbeat hands off long work, with the ``session_*`` tools and without.
_HEARTBEAT_WORKERS = (
    "Long or heavy work belongs in a worker session rather than this thread or "
    "a sub-agent: open one with session_create and give it a self-contained "
    "brief with session_send, so this thread stays free and the user can see "
    "the work in the sidebar and take it over. On your next wake, read how the "
    "worker is doing with session_read_message rather than starting the same "
    "work again. "
)
_HEARTBEAT_SMALL_STEPS = (
    "Long or heavy work goes in small steps, one piece per wake, so this thread "
    "stays free between them; note in your work ledger where you stopped, so the "
    "next wake picks it up from there. "
)
#: Both hand-offs: the cadence itself is the patrol, and a hidden turn is not
#: one a person opened, so ``monitor_start`` refuses inside it.
_HEARTBEAT_PATROL = (
    "Your next heartbeat is the patrol: it brings you back to this work on its "
    "own, so this wake ends once its step is done, with no monitor loop or "
    "wait to arm (a heartbeat turn cannot start a monitor loop). "
)


#: The gateway's line on a wake after every milestone is met. Principles, so the
#: crewmate words what it does itself.
MILESTONES_MET_LINE = (
    "Kiro Crew notes: every milestone above is met, or this goal has none yet, so "
    "the goal needs its next milestones. Met milestones are not a reason to wait, "
    "and neither is a decision the user still owes: that one stays where it is. "
    "This wake's first step is the next milestones for this goal, proposed on a "
    "card with set_key_results, each with a target a later reading can be checked "
    "against and the reason it is the next step. Work toward the goal can follow "
    "in the same wake once the card is up."
)

#: The gateway's line when every milestone is met and the next set already waits
#: on the user's button: the card is the user's step, the work is this wake's.
MILESTONES_PENDING_LINE = (
    "Kiro Crew notes: every milestone above is met, and the next milestones "
    "already wait on the user on a card, so that card is the user's step, not "
    "this wake's. This wake does a piece of work toward the goal that needs no "
    "one, so the goal keeps moving while the card waits. A quiet end fits this "
    "wake once that work is done."
)


async def milestones_line(state: Any, slot_key: str, rec: dict[str, Any]) -> str:
    """The gateway line this wake carries about the goal's milestones, or ``""``.

    Empty while the goal record has an open milestone
    (:func:`member_milestones.milestones_all_met`). Once none is open, the line
    asks for the next milestones on a card first (:data:`MILESTONES_MET_LINE`);
    when the thread already holds a milestones card waiting on the user's button,
    doing work toward the goal is the fallback (:data:`MILESTONES_PENDING_LINE`).
    A set the user applies has open milestones again, so both stay off until
    they are met. A card store that cannot be read counts as no card.
    """
    from kiro_crew.member_milestones import milestones_all_met

    if not milestones_all_met(rec):
        return ""
    from kiro_crew.change_card_catalog import KIND_CREWMATE_KEY_RESULTS
    from kiro_crew.dashboard import change_cards

    try:
        store = change_cards.card_store_for(state)
        await store.warm()
        cards = store.pending(slot_key)
    except Exception:
        logger.debug("milestones line: card store unreadable for %s", slot_key, exc_info=True)
        cards = []
    open_status = (change_cards.STATUS_PENDING, change_cards.STATUS_APPLYING)
    waiting = any(
        c.get("kind") == KIND_CREWMATE_KEY_RESULTS and c.get("status") in open_status for c in cards
    )
    return MILESTONES_PENDING_LINE if waiting else MILESTONES_MET_LINE


def heartbeat_prompt(rec: dict[str, Any], *, has_session_tools: bool, milestones: str = "") -> str:
    """The hidden turn of one heartbeat wake, built from the current goal record.

    Written as principles the crewmate phrases itself (RFC "Crewmate heartbeat"
    3.2-3.5): find what it can move forward, do it, record it where the user
    looks, and write in the chat only when the user would lose something by not
    knowing now. *has_session_tools* picks how long work is handed off
    (:func:`member_has_session_tools`). *milestones* (:func:`milestones_line`)
    goes under the progress block.
    """
    every = member_goals.interval_label(heartbeat_interval(rec["schedules"]))
    return (
        "[Heartbeat] "
        + _PREAMBLE
        + f"{member_goals.progress_block(rec)}\n\n"
        + (f"{milestones}\n\n" if milestones else "")
        + f"This is your heartbeat, a wake every {every} that lets you keep your "
        "work moving while the user is away. Look at your goals and milestones "
        "above and at the open items in your work ledger, then move forward "
        "whatever you can without the user: one thing or several, as the work "
        "calls for. Milestones that are all met, or work that waits on the user, "
        "still leave a next step that needs no one: the next improvement the goal "
        "calls for, new milestones proposed with set_key_results, or a draft the "
        "user can review and undo. That is this wake's work. Only a step that "
        "cannot be undone or that reaches outside (a push, a merge, a release, a "
        "message to someone else) waits for the user, recorded on your Dashboard "
        "tab. "
        + (_HEARTBEAT_WORKERS if has_session_tools else _HEARTBEAT_SMALL_STEPS)
        + _HEARTBEAT_PATROL
        + "Record what you did where the user looks: a tracked item's progress "
        "in your work ledger, the fields your Dashboard tab asks you to fill with "
        "dashboard_write, and a fresh milestone reading with key_result_progress. "
        "The user follows your work on your Dashboard tab, so the chat stays "
        "quiet.\n\n"
        "Write in the chat only when the user would lose something by not "
        "knowing now: you are blocked on a decision only they can make, "
        "something they asked for is done, or there is a surprise or a risk they "
        "would want to hear about at once, such as a broken build or a security "
        "problem. Everything else waits for the Weekly report. Otherwise end the "
        "turn with nothing_to_do and post nothing, which is also how a wake with "
        "nothing to move forward ends. Every word you write in this thread is "
        "shown to the user, so on a quiet end call nothing_to_do with no text "
        "before it; put any reason in its note.\n\n"
        "When you need the user's answer, ask about one thing, once, with an "
        "[OPTIONS: ...] line at the end of your message rather than an "
        "ask_question card, and leave it at that rather than asking again in "
        "other words. No answer means not now: when the step can be undone, go "
        "ahead with the safe default and note that on your Dashboard tab; when "
        "it cannot, leave it and record it on your Dashboard tab as waiting on "
        "the user."
    )


def member_has_session_tools(member: str) -> bool:
    """Whether *member*'s thread is expected to hold the ``session_*`` tools.

    The same answer the context builder gives a member turn with no live session
    evidence, when it decides whether to inject the operating-mode block
    (``context._member_dispatch_held(None, cfg)``: the configured member backend
    is one that mounts the dispatch server per session), narrowed by the
    user-level switch-off of that server, which the mount itself also honours
    (``session_mcp_server_is_disabled``). It only chooses the heartbeat's
    wording, so any failure answers False: small steps in the thread work with
    or without the tools. Blocking IO.
    """
    try:
        from kiro_crew.agent_sdk.agent_specs import session_mcp_server_is_disabled
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.context import _member_dispatch_held
        from kiro_crew.members import MEMBER_DISPATCH_SERVER

        cfg = KiroCrewConfig.load()
        if not _member_dispatch_held(None, cfg):
            return False
        binding = cfg.agents.get(member)
        agent = getattr(binding, "kiro_agent", "") or None
        return not session_mcp_server_is_disabled(MEMBER_DISPATCH_SERVER, agent)
    except Exception:
        logger.debug("session tools check failed for %r", member, exc_info=True)
        return False


def _idle_thread(state: Any, slot_key: str, member: str) -> tuple[Any, str]:
    """``(slot, "")`` when *member*'s open thread is idle, else ``(None, reason)``."""
    slot = state.get_slot(slot_key)
    if (
        slot is None
        or getattr(slot, "mode", "") != members_mod.DM_SLOT_MODE
        or getattr(slot, "agent", "") != member
        or getattr(slot, "executor", "") == "remote"
    ):
        return None, "skipped: the crewmate's thread is not open"
    if slot.running or getattr(slot, "_queue", None):
        return None, "skipped: the crewmate is busy"
    return slot, ""


async def fire(state: Any, job: Any) -> str:
    """Run one check-in for *job*'s crewmate; returns a one-line outcome for the run log."""
    from kiro_crew.dashboard.mate_welcome import dispatch_hidden_turn

    cadence = cadence_of(job)
    if cadence is None:
        return "skipped: not a goal check-in"
    slug = str(job.managed_by)[len(MANAGED_PREFIXES[cadence]) :]
    if state is None:
        return "skipped: the dashboard is not running"
    try:
        members_mod.validate_slug(slug)
        binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
    except (members_mod.MemberSlugError, OSError):
        return "skipped: no crewmate thread"
    if not binding:
        return "skipped: no crewmate thread"
    member = binding.get("member")
    if not isinstance(member, str) or not member:
        return "skipped: no crewmate thread"
    try:
        rec = await asyncio.to_thread(member_goals.read_goals, slug, member)
    except member_goals.GoalsUnreadable:
        logger.warning("goal check-in for %s: goal record unreadable", slug)
        return "skipped: goal record unreadable"
    if not rec["schedules"].get(cadence) or not member_goals.has_goal(rec):
        return "skipped: switch off or no goal"
    slot_key = binding.get("slot_key", "")
    slot, skip = _idle_thread(state, slot_key, member)
    if skip:
        return skip
    finished: list[dict[str, str]] = []
    if cadence in member_goals.REPORT_KINDS:
        finished = await asyncio.to_thread(finished_tasks, slot_key, cadence)
        # A turn (a cron notification, a user message) can start while the
        # ledger read runs; the check-in then waits for its next time.
        again, skip = _idle_thread(state, slot_key, member)
        if skip:
            return skip
        if again is not slot:
            return "skipped: the crewmate's thread is not open"
    if cadence == member_goals.CADENCE_HEARTBEAT:
        if gateway_stopping():
            return STOPPING_SKIP
        has_tools = await asyncio.to_thread(member_has_session_tools, member)
        line = await milestones_line(state, slot_key, rec)
        # The reads above await, so the thread and the gateway are checked again.
        if gateway_stopping():
            return STOPPING_SKIP
        again, skip = _idle_thread(state, slot_key, member)
        if skip:
            return skip
        if again is not slot:
            return "skipped: the crewmate's thread is not open"
        prompt = heartbeat_prompt(rec, has_session_tools=has_tools, milestones=line)
    else:
        prompt = reminder_prompt(rec, cadence, finished=finished)
    dispatch_hidden_turn(state, slot, prompt, human_seen=False)
    return "started"


#: The run-log line of a heartbeat that fired while the gateway was stopping.
STOPPING_SKIP = "skipped: the gateway is stopping"


def gateway_stopping() -> bool:
    """Whether this gateway is shutting down (the process-wide ``shutdown_event``).

    A heartbeat that fires in that window would start a model turn the stop is
    about to cancel, paying for a wake that does nothing, so it skips instead.
    """
    import kiro_crew

    try:
        return kiro_crew.shutdown_event.is_set()
    except Exception:
        logger.debug("shutdown_event unreadable", exc_info=True)
        return False


def _folder_label(label: str) -> str:
    # A path segment: "/" would nest folders, so it reads as a dash.
    clean = label.replace("/", "-").strip() or "crewmate"
    return clean[:100]


def save_report(
    slug: str, member: str, label: str, *, kind: str, title: str, markdown: str, session_key: str
) -> dict[str, Any]:
    """Save one report as a Markdown artifact under ``Crewmates/<name>/Reports``.

    Records it on the goal record and returns ``{"slug", "title", "url",
    "folder"}``. Blocking IO.
    """
    from kiro_crew.artifacts import get_default_folder_store, get_default_store

    if kind not in member_goals.REPORT_KINDS:
        raise member_goals.GoalError("invalid_report_kind", "a report is weekly or monthly")
    if not isinstance(markdown, str) or not markdown.strip():
        raise member_goals.GoalError("empty_report", "the report needs some text")
    # The crewmate wrote the report: scrubbed like every goal string it writes.
    title = member_goals.scrub_text(title)
    markdown = member_goals.scrub_text(markdown)
    folder_path = REPORTS_FOLDER.format(name=_folder_label(label))
    folder_id = get_default_folder_store().resolve_path(folder_path, create_missing=True)
    art = get_default_store().create(
        name=title,
        content=markdown,
        kind="markdown",
        source="chat",
        description=f"{_NAMES[kind]} by {label}",
        tags=["goal-report", kind],
        folder_id=folder_id,
        session_key=session_key,
    )
    member_goals.record_report(slug, member, kind=kind, artifact=art.slug, title=title)
    return {
        "slug": art.slug,
        "title": title,
        "url": f"/artifacts/{art.slug}",
        "folder": folder_path,
    }
