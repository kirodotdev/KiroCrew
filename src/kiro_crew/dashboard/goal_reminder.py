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
refuses it only on a user-facing or channel turn). The turn's text is shown in
the chat like any reply, so the prompt asks for ``nothing_to_do`` with no text
before it whenever nothing can move forward. Long work goes to a worker
session when the crewmate's member backend mounts the ``session_*`` tools
(:func:`member_has_session_tools`); otherwise it is done in small steps across
wakes. Either way the next wake is the patrol. A heartbeat that fires while the gateway is stopping
starts no turn (:func:`gateway_stopping`), and so does one that fires while a
monitor loop drives the thread (:func:`_monitor_loop_drives`). While a
milestones card of the crewmate's waits on the user, the wake proposes no new
milestones (:func:`milestones_card_waiting`). Only the owner's route and an
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

from kiro_crew import crewmate_prompts, member_goals
from kiro_crew import members as members_mod
from kiro_crew.dashboard_templates.page_look import PAGE_LOOK

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
            message=f"{_NAMES[cadence]} for {label}. It runs in the mate's own chat.",
            cron_expr=cron_expr_for(cadence, schedules),
            timezone=tz,
            silent=True,
            hide_in_chat=True,
            persistent_session=False,
            minimal_context=True,
        )


_PREAMBLE = crewmate_prompts.text("wake.preamble")


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
    return "\n".join(lines) or crewmate_prompts.text("reminder.no_finished_tasks")


#: Added to a crewmate's daily reminder and goal-setup wakes while it has no Dashboard
#: page of its own (:func:`dashboard_line`). The every-turn ``[DASHBOARD]`` block says
#: the same for a crewmate with no goal (``dashboard_agentic.SUGGESTIONS_LINE``).
DASHBOARD_BUILD_LINE: str = (
    "Kiro Crew notes: your Dashboard tab has no page of your own yet, and your "
    "progress belongs on it rather than in chat. Build one with "
    "the dashboard skill: dashboard_save stores your HTML page (view.html), and "
    "dashboard_apply puts it live. Read dashboard_types for paths and row keys: "
    "workers for sessions, work and workstreams for progress, hourly history and "
    "project spend, goal for relevant milestones, waiting for things needing the "
    "person, timeline for recent events, tools and usage for the live session's "
    "activity. Bind values through the model. A verified project snapshot can be "
    "agentic, with its source and observation time visible. "
    + PAGE_LOOK
    + " After apply, one short message says what changed and that the page is on your "
    "Dashboard tab."
)


def knows_its_page(rec: dict[str, Any]) -> bool:
    """Whether a crewmate knows what its page is about: a goal and confirmed milestones.

    Milestones are stored only once the person confirms them (the
    ``crewmate.key_results`` card or the Goals tab), so a stored one is a confirmed one.
    """
    return member_goals.has_goal(rec) and bool(rec.get("key_results"))


def dashboard_line(slug: str, rec: dict[str, Any], *, has_mate_tools: bool = True) -> str:
    """The build-your-page line for crewmate *slug*, the one-time page offer, or ``""``.

    The build line only while its Dashboard tab shows nothing of its own: no package bound to it
    (:func:`kiro_crew.dashboard_agentic.bound_package`) and the suggestions page
    (:func:`kiro_crew.dashboard_agentic.tab_shows_suggestions`, the read route's own
    answer), so a template page the person picked, or the page named for the
    crewmate, is never replaced on a wake: a crewmate on a template page gets the
    one-time offer instead (:func:`kiro_crew.dashboard.page_offer.offer_line`), a card
    the person answers, and only once *rec* holds a goal and confirmed milestones
    (:func:`knows_its_page`), since that card offers a page built around the goal.
    The build line needs no goal: a crewmate's progress goes on its page, so the
    first page is built when it has something to report. With ``agent.crew_panel`` off (:func:`kiro_crew.members.crew_panel_enabled`)
    no DM session mounts ``kirocrew-panel``, so there are no ``dashboard_save`` /
    ``dashboard_apply`` tools and neither line. The offer needs
    ``offer_dashboard_page`` (``kirocrew-mate``), so a crewmate without those tools
    (*has_mate_tools* False) gets no offer line. An empty *slug*, or a lookup that
    fails, gets no line. Blocking IO.
    """
    if not slug:
        return ""
    from kiro_crew import dashboard_agentic

    try:
        from kiro_crew.dashboard_templates import instance as template_instance
        from kiro_crew.members import crew_panel_enabled

        if not crew_panel_enabled():
            return ""
        if template_instance.is_locked(slug):
            # The person locked the layout: an apply would be refused, so no wake
            # asks for a page.
            return ""
        if dashboard_agentic.bound_package(slug):
            return ""
        if not dashboard_agentic.tab_shows_suggestions(slug):
            # A template page is on the tab: never replaced on a wake, only offered,
            # once, on a card (``page_offer``).
            from kiro_crew.dashboard import page_offer

            return page_offer.offer_line(slug, rec) if has_mate_tools else ""
    except Exception:
        logger.debug("dashboard line for %s: page lookup failed", slug, exc_info=True)
        return ""
    return DASHBOARD_BUILD_LINE


#: How a weekly report or monthly review looks: one piece both prompts share, and
#: the one ``save_goal_report``'s description points at. Principles the crewmate
#: phrases itself; the look is its own, inside them. The viewer opens the page in
#: the crewmate Dashboard page's sandbox, so it reaches no network and loads no
#: library, which is why a chart is inline SVG. A page embeds no raster image, so
#: it takes the plain credential scrub on save and read and loses nothing to it.
REPORT_LOOK = (
    "How the report looks: send it as html, one whole self-contained page that "
    "reads like a printed document a team lead hands round as a PDF, top to "
    "bottom, rather than an app screen; markdown stays available for a plain one. "
    "Lay it out on A4 sheets (@page { size: A4; margin: 0 }, each sheet 210mm by "
    "297mm) with a strong header band, numbered sections, and a footer on each "
    "sheet carrying your name, the date range and the page number. A sheet has a "
    "fixed height, so plan what each one holds: a section that would run past a "
    "sheet's bottom or into its footer starts on the next sheet, and a chart takes a height that "
    "fits. Break pages between sheets so nothing is cut at a sheet's edge, and keep "
    "print-color-adjust: exact so the colours survive printing. Open with the "
    "conclusion of the period, readable at a glance. Give each section the shape "
    "of its content: a comparison as a table, a flow as steps, movement over time "
    "as a chart drawn from the readings, a blocker as a card that says who it "
    "waits on, rather than a wall of bullets. Keep one visual system across the "
    "page, a type scale, a spacing rhythm and a small palette on light paper, so "
    "it reads well on screen and on paper. Give each colour one meaning across the "
    "page and pair it with a word, so the page still reads in grey or for someone "
    "who cannot tell the colours apart. For type, build every family from the "
    "CSS generic families alone (system-ui, sans-serif; ui-serif, serif; "
    "ui-monospace, monospace) and name no typeface, so each system draws its own "
    "face; when the page has its own :root, define them there as --font-sans, "
    "--font-serif and --font-mono. Two families at most, each with a clear job: "
    "serif headings with sans for body and tables, or sans for both; mono only for "
    "code, ids and aligned figures, and not for body text or headings. Build the "
    "hierarchy with size, weight and colour rather than more families, align "
    "figures with font-variant-numeric: tabular-nums, and keep text in any one "
    "script in one style; the generic families already pick each script's system "
    "face. The page is a snapshot: its numbers are "
    "the readings at the time you wrote it, so it needs no live data, no scripts "
    "and no motion; draw every chart as inline SVG, and embed no raster image "
    "(no <img>, no data:image), since the page opens with no network. Links on "
    "the page stay inert, so name a pull request or a task in plain text. Use plain "
    "words, without internal ids "
    "or file paths: the reader knows only the page, so a word like reading "
    "gets a short explanation where it first appears (one recorded value of a key "
    "result, with its date), or is left out. Call each key result by one name and "
    "measure it over one time span everywhere it appears, so a table, a card and a "
    "chart are plainly the same measure, and a chart's axis ends on the report's "
    "last day, with the date of the last reading stated. Call the period by the "
    "one word the report uses (a week is a week, not a sprint). Give each status "
    "label its own word, one the page uses for nothing "
    "else, with a short legend where the labels first appear; a colour named in a "
    "key is the colour drawn. A blocked item names the person or outside event it "
    "waits on, or reads like Waits on: nothing outside me. Use only real "
    "evidence: real states and "
    "real counts that agree "
    "with the readings above, with no invented percentage or made-up time. Your "
    "own style is welcome; a style the user has asked for wins."
)


#: The daily wake's review of the crewmate's own Dashboard page, in steps: what it
#: reads, what it checks, what it updates, and one improvement. Each step is its own
#: sentence so a later step (a look at a screenshot of the page) slots in before the
#: check.
DAILY_PAGE_REVIEW: str = (
    "Then review your Dashboard page, when it shows a page of your own. Read it: "
    "dashboard_fields gives each field's source and its current value or summary, and "
    "dashboard_types the folds and paths behind it, so you compare what the page "
    "shows with the latest data. Check that the page as a whole still makes sense: "
    "it answers what the person would ask about your work today, its sections and "
    "words match what you are working on now, and no figure or phrase contradicts "
    "the folds. Update what is stale: a field of yours with dashboard_write, and "
    "the page itself with dashboard_save then dashboard_apply when a section no "
    "longer fits the work; leave a page that is current as it is. Then look for one "
    "way to make the page better -- clearer to read, better made, nicer to look at -- "
    "following the dashboard skill's design guidance, and make it: one small, steady "
    "improvement a day, not a redesign, and every number still read from a fold, "
    "none invented."
)


def _page_tools_on() -> bool:
    """Whether the crewmate's sessions mount ``kirocrew-panel``; ``False`` when the
    switch cannot be read, so a wake that cannot tell sends no status to a page."""
    try:
        from kiro_crew.members import crew_panel_enabled

        return bool(crew_panel_enabled())
    except Exception:
        logger.debug("goal reminder: the crew panel switch is unreadable", exc_info=True)
        return False


def reminder_prompt(
    rec: dict[str, Any],
    cadence: str = member_goals.CADENCE_DAILY,
    *,
    finished: list[dict[str, str]] | None = None,
    dashboard: str = "",
    page_tools: bool = True,
) -> str:
    """The hidden turn's text for *cadence*, built from the current goal record.

    *finished* is :func:`finished_tasks` for the weekly and monthly prompts.
    *dashboard* is :func:`dashboard_line`, added last to the daily prompt.
    *page_tools* says the crewmate's sessions mount ``kirocrew-panel``
    (``members.crew_panel_enabled``): only then does the daily prompt send its
    status to the Dashboard page (:data:`DAILY_PAGE_REVIEW`); without the page tools
    the readings lead its chat message, as there is no page to put them on.
    """
    block = member_goals.progress_block(rec)
    done = _finished_lines(finished or [])
    if cadence == member_goals.CADENCE_DAILY:
        return (
            "[Daily goal reminder] " + _PREAMBLE + f"{block}\n\n"
            "Take a fresh reading of each key result and record it with "
            "key_result_progress (on the kirocrew-mate server); a reading can be a "
            "number or a short note. "
            + (
                "Where things stand toward each goal belongs on your Dashboard page, "
                "not in a chat message. "
                + DAILY_PAGE_REVIEW
                + " Write in this thread only when something changed that the "
                "user should know, and then once, covering all of it; then continue "
                "with your next task."
                if page_tools
                else "Lead your message with those readings and say briefly where "
                "things stand toward each goal, then continue with your next task."
            )
            + " Ask the user something only when you are blocked."
        ) + (f"\n\n{dashboard}" if dashboard else "")
    if cadence == member_goals.CADENCE_WEEKLY:
        return (
            "[Weekly report] "
            + _PREAMBLE
            + f"{block}\n\nHow each key result moved this week, from its readings:\n"
            + _movement_lines(rec, cadence)
            + "\n\nTasks you finished this week, from your work ledger:\n"
            + done
            + "\n\nWrite this week's report for the user: how each key "
            "result moved, the tasks you finished, what is stuck and why, "
            "and the plan for next week. Base the numbers on the readings above, and "
            "take a fresh reading first when one is stale. "
            + REPORT_LOOK
            + ' Save it with save_goal_report (kind "weekly"), then reply in the thread '
            "with one short line pointing at it; the report itself stays out of the chat."
        )
    return (
        "[Monthly review] "
        + _PREAMBLE
        + f"{block}\n\nHow each key result moved this month, from its readings:\n"
        + _movement_lines(rec, cadence)
        + "\n\nTasks you finished this month, from your work ledger:\n"
        + done
        + "\n\nReview whether your goals and their key results still fit the "
        "work you are actually doing. Write the review for the user: what the month "
        "showed, whether the direction still holds, and what you would change. "
        + REPORT_LOOK
        + ' Save it with save_goal_report (kind "monthly"), then reply in the thread '
        "with one short line pointing at it. When the key results or a goal no longer fit the "
        "work, propose the replacement as a card in this turn (set_key_results for the "
        "whole set, set_goal with that goal's id for a goal) with the reason drawn from "
        "the review; the user decides with the card's button. When they still fit, "
        "leave them as they "
        "are and propose nothing."
    )


#: How a heartbeat hands off long work, with the ``session_*`` tools and without.
#: The hand-off is the crewmate's own worker, and its report wakes this thread on its own
#: (``dashboard/mate_thread_wakes.py``), so the wake ends instead of polling.
_HEARTBEAT_WORKERS_V2 = (
    "Long or heavy work belongs to your worker, not this thread: open it with "
    "session_create and give it a self-contained brief with session_send, so "
    "this thread stays free and the user can see the work in the sidebar and "
    "take it over. Then end this wake: the worker's report comes back to this "
    "thread on its own as a [Worker report] turn, and that turn or a later wake "
    "picks the work up from there rather than starting it again. "
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


#: The next-step list's milestones clause, offered only while no milestones card
#: of the crewmate's waits on the user (:func:`milestones_card_waiting`).
_HEARTBEAT_NEW_MILESTONES = "new milestones proposed with set_key_results, "
#: The line that replaces it while such a card waits: the card is the user's
#: step, so a wake proposes no second one and spends no credits re-asking.
_HEARTBEAT_MILESTONES_WAITING = (
    "A milestones card you proposed already waits on the user, so this wake "
    "proposes no new milestones. "
)


def _heartbeat_handoff() -> str:
    """The hand-off line for a thread with the ``session_*`` tools."""
    return _HEARTBEAT_WORKERS_V2


def heartbeat_prompt(
    rec: dict[str, Any], *, has_session_tools: bool, milestones_waiting: bool = False
) -> str:
    """The hidden turn of one heartbeat wake, built from the current goal record.

    Written as principles the crewmate phrases itself (RFC "Crewmate heartbeat"
    3.2-3.5): find what it can move forward, do it, record it where the user
    looks, and write in the chat only when the user would lose something by not
    knowing now. *has_session_tools* picks how long work is handed off
    (:func:`member_has_session_tools`). *milestones_waiting* (a milestones card
    of the crewmate's still waits on the user, :func:`milestones_card_waiting`)
    drops new milestones from the next steps and says why.
    """
    every = member_goals.interval_label(heartbeat_interval(rec["schedules"]))
    new_milestones = "" if milestones_waiting else _HEARTBEAT_NEW_MILESTONES
    waiting = _HEARTBEAT_MILESTONES_WAITING if milestones_waiting else ""
    return (
        "[Heartbeat] "
        + _PREAMBLE
        + f"{member_goals.progress_block(rec)}\n\n"
        + "When nothing above can move forward, call nothing_to_do at once, with no "
        "text before it.\n\n"
        + f"This is your heartbeat, a wake every {every} that lets you keep your "
        "work moving while the user is away. Look at your goals and milestones "
        "above and at the open items in your work ledger, then move forward "
        "whatever you can without the user: one thing or several, as the work "
        "calls for. Milestones that are all met, or work that waits on the user, "
        "still leave a next step that needs no one: the next improvement the goal "
        f"calls for, {new_milestones}or a draft the "
        "user can review and undo. " + waiting + "That is this wake's work. Only a step that "
        "cannot be undone or that reaches outside (a push, a merge, a release, a "
        "message to someone else) waits for the user, recorded on your Dashboard "
        "tab. "
        + (_heartbeat_handoff() if has_session_tools else _HEARTBEAT_SMALL_STEPS)
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
        return None, "skipped: the mate's thread is not open"
    if slot.running or getattr(slot, "_queue", None):
        return None, "skipped: the mate is busy"
    return slot, ""


async def fire(state: Any, job: Any) -> str:
    """Run one check-in for *job*'s crewmate; returns a one-line outcome for the run log."""
    from kiro_crew.dashboard.mate_welcome import (
        dispatch_hidden_turn,
        hidden_turn_language,
        with_language,
    )

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
        return "skipped: no mate thread"
    if not binding:
        return "skipped: no mate thread"
    member = binding.get("member")
    if not isinstance(member, str) or not member:
        return "skipped: no mate thread"
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
    dashboard = ""
    page_tools = True
    if cadence == member_goals.CADENCE_DAILY:
        from kiro_crew.member_mate_tools import member_has_mate_tools

        dashboard = await asyncio.to_thread(
            lambda: dashboard_line(slug, rec, has_mate_tools=member_has_mate_tools(member))
        )
        page_tools = await asyncio.to_thread(_page_tools_on)
        # The binding read ran off the loop: a turn may have started meanwhile.
        again, skip = _idle_thread(state, slot_key, member)
        if skip:
            return skip
        if again is not slot:
            return "skipped: the crewmate's thread is not open"
    if cadence in member_goals.REPORT_KINDS:
        finished = await asyncio.to_thread(finished_tasks, slot_key, cadence)
        # A turn (a cron notification, a user message) can start while the
        # ledger read runs; the check-in then waits for its next time.
        again, skip = _idle_thread(state, slot_key, member)
        if skip:
            return skip
        if again is not slot:
            return "skipped: the mate's thread is not open"
    if cadence == member_goals.CADENCE_HEARTBEAT:
        has_tools = await asyncio.to_thread(member_has_session_tools, member)
        waiting = await milestones_card_waiting(state, slot_key)
        # The cron timer stops firing once the gateway stops; this one check
        # covers a stop that began during the reads above, as does the thread's.
        if gateway_stopping():
            return STOPPING_SKIP
        again, skip = _idle_thread(state, slot_key, member)
        if skip:
            return skip
        if again is not slot:
            return "skipped: the mate's thread is not open"
        if _monitor_loop_drives(slot_key):
            return LOOP_SKIP
        prompt = heartbeat_prompt(rec, has_session_tools=has_tools, milestones_waiting=waiting)
    else:
        prompt = reminder_prompt(
            rec, cadence, finished=finished, dashboard=dashboard, page_tools=page_tools
        )
    # No tab opened this turn, so only the configured language is known; it
    # replaces the preamble's language sentence (mate_welcome.with_language).
    language = await asyncio.to_thread(hidden_turn_language)
    if gateway_stopping():
        return STOPPING_SKIP
    again, skip = _idle_thread(state, slot_key, member)
    if skip:
        return skip
    if again is not slot:
        return "skipped: the mate's thread is not open"
    dispatch_hidden_turn(
        state, slot, with_language(prompt, language, in_thread=True), human_seen=False
    )
    return "started"


#: The run-log line of a heartbeat that fired while the gateway was stopping.
STOPPING_SKIP = "skipped: the gateway is stopping"

#: The run-log line of a heartbeat that fired while a monitor loop drives the thread.
LOOP_SKIP = "skipped: a monitor loop is running"


def _monitor_loop_drives(slot_key: str) -> bool:
    """Whether a monitor loop that will fire again is bound to *slot_key*'s thread.

    Between two of its cycles the thread is idle, so the idle check alone would
    let a heartbeat wake into work the loop already drives. The answer is the
    work ledger's own (``ledger_wake.worker_looping``: the bare, ``dashboard_``
    and channel keys, an active loop with budget left); anything unreadable
    reads as no loop, so the wake runs as it would without one.
    """
    from kiro_crew import ledger_wake
    from kiro_crew.autonudge import get_instance

    return ledger_wake.worker_looping(get_instance(), slot_key)


async def milestones_card_waiting(state: Any, slot_key: str) -> bool:
    """Whether a milestones card the crewmate proposed still waits on the user.

    A ``crewmate.key_results`` card in the thread that is pending or applying.
    While one waits, the wake's prompt drops new milestones from its next steps
    (:func:`heartbeat_prompt`), so a wake does not propose a second card the
    user has not answered. A card store that cannot be read counts as no card.
    """
    from kiro_crew.change_card_catalog import KIND_CREWMATE_KEY_RESULTS
    from kiro_crew.dashboard import change_cards

    try:
        store = change_cards.card_store_for(state)
        await store.warm()
        cards = store.pending(slot_key)
    except Exception:
        logger.debug("milestones card check: store unreadable for %s", slot_key, exc_info=True)
        return False
    open_status = (change_cards.STATUS_PENDING, change_cards.STATUS_APPLYING)
    return any(
        c.get("kind") == KIND_CREWMATE_KEY_RESULTS and c.get("status") in open_status for c in cards
    )


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
    slug: str,
    member: str,
    label: str,
    *,
    kind: str,
    title: str,
    session_key: str,
    markdown: str = "",
    html: str = "",
) -> dict[str, Any]:
    """Save one report as an artifact under ``Crewmates/<name>/Reports``.

    The report is either *markdown* (a ``markdown`` artifact) or *html*, a whole
    self-contained page (an ``html`` artifact), and exactly one of the two is
    given. Both are marked :attr:`~kiro_crew.artifact_store.model.Artifact.goal_report`,
    so the viewer opens the page in the crewmate Dashboard page's sandbox. Records
    it on the goal record and returns ``{"slug", "title", "url", "folder",
    "format"}``. Blocking IO.
    """
    from kiro_crew.artifacts import get_default_folder_store, get_default_store

    if kind not in member_goals.REPORT_KINDS:
        raise member_goals.GoalError("invalid_report_kind", "a report is weekly or monthly")
    given = [
        (fmt, text)
        for fmt, text in (("markdown", markdown), ("html", html))
        if isinstance(text, str) and text.strip()
    ]
    if not given:
        raise member_goals.GoalError(
            "empty_report", "the report needs some text, as markdown or as html"
        )
    if len(given) > 1:
        raise member_goals.GoalError(
            "report_format_ambiguous", "send the report as markdown or as html, not both"
        )
    ((fmt, body),) = given
    if fmt == "html":
        size = len(body.encode("utf-8"))
        if size > member_goals.REPORT_HTML_MAX_BYTES:
            raise member_goals.GoalError(
                "report_too_large",
                f"the html report is {size} bytes, over the "
                f"{member_goals.REPORT_HTML_MAX_BYTES}-byte limit",
            )
    # The crewmate wrote the report: scrubbed like every goal string it writes,
    # in either format. A page draws its charts as inline SVG and embeds no raster
    # image, so the plain scrub costs it nothing.
    title = member_goals.scrub_text(title)
    body = member_goals.scrub_text(body)
    folder_path = REPORTS_FOLDER.format(name=_folder_label(label))
    folder_id = get_default_folder_store().resolve_path(folder_path, create_missing=True)
    art = get_default_store().create(
        name=title,
        content=body,
        kind=fmt,
        source="chat",
        description=f"{_NAMES[kind]} by {label}",
        tags=["goal-report", kind],
        folder_id=folder_id,
        session_key=session_key,
        goal_report=True,
    )
    member_goals.record_report(slug, member, kind=kind, artifact=art.slug, title=title)
    return {
        "slug": art.slug,
        "title": title,
        "url": f"/artifacts/{art.slug}",
        "folder": folder_path,
        "format": fmt,
    }
