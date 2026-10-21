"""A crewmate's check-ins: Daily Goal Reminder, Weekly report, Monthly review, Heartbeat.

The Goals tab's four switches (``PUT /api/members/{slug}/goals/schedules``) are
stored in the goal record (:mod:`kiro_crew.member_goals`), and each switch keeps
exactly ONE gateway-owned schedule in step with it, through the cron store's
installer key (``managed_by``, ``goal-<cadence>:<slug>``), so no switch can ever
have two schedules and none touches another's:

* on -> that cadence's old schedule (if any) is removed and a fresh one created
  at the shared time of day: daily ``M H * * *``, weekly ``M H * * <weekday>``,
  monthly ``M H <day> * *``, in the user's timezone; the heartbeat runs on its
  interval instead (``*/15 * * * *``, ``*/30 * * * *``, ``0 * * * *``,
  ``0 */2 * * *``, ``0 */4 * * *``, :func:`cron_expr_for`);
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
as a chat message (``dispatch_hidden_turn(heartbeat=True)``, settled by
:mod:`kiro_crew.dashboard.heartbeat_quiet_end`). Long work goes to a worker
session when the crewmate's member backend mounts the ``session_*`` tools
(:func:`member_has_session_tools`); otherwise it is done in small steps across
wakes. Either way the next wake is the patrol. Only the owner's route and an applied card write the switch, so the
crewmate cannot turn its own heartbeat on.

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
    15: "*/15 * * * *",
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


def reminder_prompt(
    rec: dict[str, Any],
    cadence: str = member_goals.CADENCE_DAILY,
    *,
    finished: list[dict[str, str]] | None = None,
    gateway_lines: str = "",
) -> str:
    """The hidden turn's text for *cadence*, built from the current goal record.

    *finished* is :func:`finished_tasks` for the weekly and monthly prompts.
    *gateway_lines* (:func:`gateway_lines`) goes under the daily reminder's
    progress block.
    """
    block = member_goals.progress_block(rec)
    done = _finished_lines(finished or [])
    if cadence == member_goals.CADENCE_DAILY:
        return crewmate_prompts.fill(
            "reminder.daily",
            preamble=_PREAMBLE,
            block=block,
            gateway_lines=f"{gateway_lines}\n\n" if gateway_lines else "",
        )
    return crewmate_prompts.fill(
        "reminder.weekly" if cadence == member_goals.CADENCE_WEEKLY else "reminder.monthly",
        preamble=_PREAMBLE,
        block=block,
        movement=_movement_lines(rec, cadence),
        done=done,
    )


def _interval_words(minutes: int) -> str:
    """``"30 minutes"`` / ``"hour"`` / ``"2 hours"``, for "every ..." in the prompt."""
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    return "hour" if hours == 1 else f"{hours} hours"


#: How a heartbeat hands off long work, with the ``session_*`` tools and without.
_HEARTBEAT_WORKERS = crewmate_prompts.text("heartbeat.handoff_workers")
_HEARTBEAT_SMALL_STEPS = crewmate_prompts.text("heartbeat.handoff_small_steps")
#: Both hand-offs: the cadence itself is the patrol, and a hidden turn is not
#: one a person opened, so ``monitor_start`` refuses inside it.
_HEARTBEAT_PATROL = crewmate_prompts.text("heartbeat.patrol")


#: The gateway's own line on a wake after every milestone is met. Principles,
#: so the crewmate words what it does itself.
MILESTONES_MET_LINE = crewmate_prompts.text("milestones.all_met")

#: The gateway's line when every milestone is met and the next set already
#: waits on the user's button: the card is the user's step, the work is this
#: wake's.
MILESTONES_PENDING_LINE = crewmate_prompts.text("milestones.card_waiting")


async def milestones_line(state: Any, slot_key: str, rec: dict[str, Any]) -> str:
    """The gateway line this wake carries about the goal's milestones, or ``""``.

    Empty while the goal record has an open milestone
    (:func:`member_goals.milestones_all_met`). Once none is open, the line asks
    for the next milestones on a card first (:data:`MILESTONES_MET_LINE`); when
    the thread already holds a milestones card waiting on the user's button,
    doing work toward the goal is the fallback (:data:`MILESTONES_PENDING_LINE`).
    A set the user applies has open milestones again, so both stay off until
    they are met. A card store that cannot be read counts as no card.
    """
    if not member_goals.milestones_all_met(rec):
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


def dashboard_line(slug: str) -> str:
    """The build-your-dashboard line while crewmate *slug* has no page of its own, else ``""``.

    A page of its own is a ``kind="dashboard"`` package bound to the crewmate
    (:func:`kiro_crew.dashboard_agentic.bound_package`, the lookup the Dashboard tab
    reads), so the line stops on the wake after the crewmate's first
    ``dashboard_apply``. An empty or unknown *slug* gets no line, and neither does
    any crewmate while Crewmate 2.0 is off (``kiro_crew.crewmate_v2``). Blocking IO.
    """
    from kiro_crew import crewmate_v2

    if not slug or not crewmate_v2.enabled():
        return ""
    from kiro_crew import dashboard_agentic

    if dashboard_agentic.bound_package(slug):
        return ""
    return crewmate_prompts.text("dashboard.build_your_page")


async def gateway_lines(state: Any, slot_key: str, rec: dict[str, Any], slug: str = "") -> str:
    """The gateway's own lines for one heartbeat or daily wake, joined, or ``""``.

    Each is a fact the gateway reads from the record rather than one the prompt
    hopes the crewmate notices: the milestones line (:func:`milestones_line`) and,
    while the crewmate has no Dashboard page of its own, the build line
    (:func:`dashboard_line`).
    """
    lines = [
        await milestones_line(state, slot_key, rec),
        await asyncio.to_thread(dashboard_line, slug),
    ]
    return "\n\n".join(line for line in lines if line)


def heartbeat_prompt(
    rec: dict[str, Any], *, has_session_tools: bool, gateway_lines: str = ""
) -> str:
    """The hidden turn of one heartbeat wake, built from the current goal record.

    Written as principles the crewmate phrases itself (RFC "Crewmate heartbeat"
    3.2-3.5): find what it can move forward, do it, record it where the user
    looks, and write in the chat only when the user would lose something by not
    knowing now. *has_session_tools* picks how long work is handed off
    (:func:`member_has_session_tools`). *gateway_lines* (:func:`gateway_lines`)
    goes under the progress block.
    """
    every = _interval_words(heartbeat_interval(rec["schedules"]))
    return crewmate_prompts.fill(
        "heartbeat",
        preamble=_PREAMBLE,
        block=member_goals.progress_block(rec),
        gateway_lines=f"{gateway_lines}\n\n" if gateway_lines else "",
        every=every,
        handoff=_HEARTBEAT_WORKERS if has_session_tools else _HEARTBEAT_SMALL_STEPS,
        patrol=_HEARTBEAT_PATROL,
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
    lines = ""
    if cadence in (member_goals.CADENCE_HEARTBEAT, member_goals.CADENCE_DAILY):
        lines = await gateway_lines(state, slot_key, rec, slug)
    if cadence == member_goals.CADENCE_HEARTBEAT:
        has_tools = await asyncio.to_thread(member_has_session_tools, member)
        again, skip = _idle_thread(state, slot_key, member)
        if skip:
            return skip
        if again is not slot:
            return "skipped: the crewmate's thread is not open"
        prompt = heartbeat_prompt(rec, has_session_tools=has_tools, gateway_lines=lines)
    else:
        if lines:
            again, skip = _idle_thread(state, slot_key, member)
            if skip:
                return skip
            if again is not slot:
                return "skipped: the crewmate's thread is not open"
        prompt = reminder_prompt(rec, cadence, finished=finished, gateway_lines=lines)
    started = dispatch_hidden_turn(
        state,
        slot,
        prompt,
        human_seen=False,
        heartbeat=cadence == member_goals.CADENCE_HEARTBEAT,
    )
    return "started" if started is not False else "skipped: the app is stopping"


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
