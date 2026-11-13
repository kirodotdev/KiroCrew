"""Wake a Bot when fields it writes on its Dashboard page go out of date.

Preview ``dashboard_stale_wake`` (needs ``dashboard_stale_fields``). The out-of-date
mark (``dashboard_agentic.stale_fields``) reaches a Bot only inside a turn: the
``[DASHBOARD]`` block of a turn someone started, or a Daily Goal Reminder or Heartbeat
(``goal_reminder.fire``). A Bot with neither check-in on is never woken, so its page
stays out of date until someone talks to it. This module closes that gap.

The schedule is ONE gateway-owned cron job (installer key :data:`MANAGED_KEY`, every
:data:`TICK_EXPR`), the cron store's own installer contract that the goal check-ins use
(:func:`sync_job`): no scheduler of its own. It exists only while the preview is on;
the config watcher installs or removes it when the switch moves, the gateway matches it
once when its scheduler starts, and a tick that finds the preview off removes it. When it fires, the gateway's cron callback hands the job
here (:func:`fire`), which looks at every Bot and, for one whose fields are due,
starts one hidden turn in its own thread through the goal check-ins' path
(``goal_reminder._load_thread``, ``mate_welcome.dispatch_hidden_turn``).

Which fields are due (:func:`due_fields`) is read against a small book on disk
(:data:`BOOK_NAME`), one row per Bot: the write stamp each stale field went stale
from when the Bot was last woken for it, and when that was. So:

* one wake per field per stale period: a field already woken for, with the same
  stamp, is not due again; a write gives it a new stamp, which re-arms it;
* fields that go stale together share one wake, which names every stale field;
* :data:`FLOOR_S` between two wakes of one Bot, whatever is due.

A Bot that is mid-turn, has a turn queued in its thread, is held by a crew pause, or
is driven by a monitor loop is not woken; it is tried again on the next tick. Every wake is recorded on the
Bot's crew log as ``dashboard/stale_wake`` (actor ``gateway``, reason
``stale_fields``, the field names), since each one is a model turn the person pays for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from kiro_crew import crewmate_prompts
from kiro_crew import members as members_mod
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: The feature preview that turns the wake on. It needs ``dashboard_stale_fields``.
PREVIEW: str = "dashboard_stale_wake"
#: The cron store's installer key of the one job that drives every wake.
MANAGED_KEY: str = "dashboard-stale-wake"
#: How often the job looks: a field goes stale on the hour scale, so ten minutes late
#: at most is close enough, and a quiet tick reads only warm folds.
TICK_EXPR: str = "*/10 * * * *"
#: Shortest gap between two wakes of one Bot, whatever goes stale in between.
FLOOR_S: int = 30 * 60
#: The book of past wakes, under the data home.
BOOK_NAME: str = "dashboard-stale-wake.json"
#: The largest book read back; a bigger file is read as damaged and started over.
_BOOK_MAX_BYTES = 256 * 1024

_ACTOR = "dashboard"
_SOURCE = "dashboard_stale_wake"

_PREAMBLE = crewmate_prompts.text("wake.preamble")


def switch_state() -> bool | None:
    """Whether :data:`PREVIEW` and ``dashboard_stale_fields`` are both on, from one
    config read; ``None`` when that read cannot say (a load that fails, or a config
    file or its ``dashboard`` section that did not parse). TOTAL."""
    try:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

        cfg = KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 -- an unreadable config says nothing
        return None
    if {DEGRADED_WHOLE_CONFIG, "dashboard"} & set(cfg.degraded_sections):
        return None
    from kiro_crew.edition_locks import feature_previews

    previews = feature_previews(cfg.dashboard.feature_previews)
    if not isinstance(previews, dict):
        return False
    return previews.get("dashboard_stale_fields") is True and previews.get(PREVIEW) is True


def enabled() -> bool:
    """Whether the wake is on; an unreadable config reads as off. TOTAL."""
    return switch_state() is True


def is_stale_wake_job(job: Any) -> bool:
    """Whether *job* is the gateway's stale-wake job, which no schedule list shows."""
    return getattr(job, "managed_by", "") == MANAGED_KEY


async def sync_job(crons: Any) -> None:
    """Make the cron store hold the stale-wake job exactly while :func:`enabled`."""
    if crons is None:
        return
    state = await asyncio.to_thread(switch_state)
    if state is None:
        # A config that cannot be read says nothing about the switch: keep the job
        # as it is rather than remove a job the person has on.
        return
    wanted = state
    jobs = [j for j in await crons.list_jobs_async(include_disabled=True) if is_stale_wake_job(j)]
    # An unreadable store loads as no jobs: refuse rather than add a second job over it.
    crons.raise_if_store_unreadable()
    if wanted and len(jobs) == 1 and jobs[0].enabled:
        return
    for job in jobs:
        await crons.remove_job_async(job.id, actor=_ACTOR, source=_SOURCE)
    if not wanted:
        return
    await crons.add_managed_job_async(
        MANAGED_KEY,
        name="Dashboard: wake Bots with out-of-date fields",
        message="Wakes a Bot whose Dashboard fields went out of date.",
        cron_expr=TICK_EXPR,
        silent=True,
        hide_in_chat=True,
        persistent_session=False,
        minimal_context=True,
    )


async def sync_job_logged(crons: Any) -> None:
    """:func:`sync_job` for a caller that must not raise: a failure is logged, and the
    next switch move or gateway start tries again."""
    try:
        await sync_job(crons)
    except Exception:
        logger.warning("dashboard stale wake: syncing its job failed", exc_info=True)


#: The start-up syncs in flight, held so a task is not collected before it runs.
_SYNC_TASKS: set[asyncio.Task[None]] = set()


def schedule_sync(crons: Any) -> None:
    """Run :func:`sync_job_logged` once in the background, off the caller's path.

    For the scheduler's start: the switch may already be on (or off) then, and the
    config watcher only reports a switch that moves. With it off, a job left from
    before is removed here: its own tick cannot do it when the fire-time gate refuses
    the run. Must run on the event loop.
    """
    task = asyncio.get_running_loop().create_task(sync_job_logged(crons))
    _SYNC_TASKS.add(task)
    task.add_done_callback(_SYNC_TASKS.discard)


def due_fields(row: dict[str, Any] | None, stale: list[tuple[str, str]], now: float) -> list[str]:
    """The stale fields that call for a wake now, or ``[]``.

    *stale* is ``[(name, written_at), ...]`` (``dashboard_agentic.member_stale_state``)
    and *row* the Bot's book row, ``{"at": last wake, "fields": {name: written_at}}``.
    A field is due when the book holds a different stamp for it (a new stale period),
    or none. Nothing is due within :data:`FLOOR_S` of the last wake. When anything is
    due, every stale field is returned, so one wake covers them all.
    """
    if not stale:
        return []
    seen = row.get("fields") if isinstance(row, dict) else None
    seen = seen if isinstance(seen, dict) else {}
    if all(seen.get(name) == stamp for name, stamp in stale):
        return []
    last = row.get("at") if isinstance(row, dict) else None
    if isinstance(last, (int, float)) and now - last < FLOOR_S:
        return []
    return sorted(name for name, _stamp in stale)


def woken_row(stale: list[tuple[str, str]], now: float) -> dict[str, Any]:
    """The book row after a wake for *stale* at *now*."""
    return {"at": now, "fields": {name: stamp for name, stamp in stale}}


def wake_prompt(line: str) -> str:
    """The hidden turn of one stale wake: the out-of-date line, then what to do."""
    return (
        "[Dashboard] " + _PREAMBLE + line + "\n\n" + crewmate_prompts.text("dashboard.stale_wake")
    )


def _book_path() -> Any:
    return data_home() / BOOK_NAME


def read_book() -> dict[str, dict[str, Any]]:
    """The book of past wakes, ``{}`` when absent or damaged. Blocking IO. TOTAL."""
    try:
        raw = _book_path().read_bytes()
    except OSError:
        return {}
    if len(raw) > _BOOK_MAX_BYTES:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def write_book(book: dict[str, dict[str, Any]]) -> bool:
    """Store *book*; whether it was stored. Blocking IO; a failure is logged.

    A book over :data:`_BOOK_MAX_BYTES` is refused here, since :func:`read_book` would
    read it back as empty and every Bot would be woken again.
    """
    body = json.dumps(book, sort_keys=True)
    if len(body.encode("utf-8")) > _BOOK_MAX_BYTES:
        logger.warning("dashboard stale wake: the book is over its size cap; not saved")
        return False
    try:
        atomic_write(_book_path(), body)
    except OSError:
        logger.warning("dashboard stale wake: the book could not be saved", exc_info=True)
        return False
    return True


class BookUnwritable(Exception):
    """The book could not be saved, so no wake may start: the book is what keeps the
    floor and the one-wake-per-period rule, and a wake it does not record repeats."""


def bots() -> list[tuple[str, str, str]]:
    """Every Bot with a DM thread, as ``(slug, member, slot_key)``. Blocking IO. TOTAL.

    Read from the crew config, so a Bot that was removed is never woken.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
    except Exception:
        logger.debug("dashboard stale wake: config unreadable", exc_info=True)
        return []
    out = []
    for name in sorted(cfg.agents):
        if not members_mod.is_dispatchable_member_name(name):
            continue
        try:
            slug = members_mod.member_slug(name, cfg)
        except members_mod.MemberSlugError:
            continue
        binding = members_mod.read_dm_binding(slug)
        if not binding:
            continue
        member = binding.get("member")
        slot_key = binding.get("slot_key")
        if isinstance(member, str) and member and isinstance(slot_key, str) and slot_key:
            out.append((slug, member, slot_key))
    return out


def _record(slug: str, member: str, fields: list[str]) -> None:
    """Record one wake on the Bot's crew log. BEST-EFFORT, blocking IO."""
    try:
        from kiro_crew import dashboard_agentic
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.dashboard.handlers.member_dashboard import _newest_unit

        slot = dashboard_agentic._member_slot(member, slug)
        unit = _newest_unit(slot) if slot else ""
        if not unit:
            return
        crew_log_emit.on_dashboard_stale_wake(
            unit, {"actor": "gateway", "reason": "stale_fields", "fields": fields}
        )
    except Exception:
        logger.debug("dashboard stale wake: %s's wake was not recorded", slug, exc_info=True)


async def _wake_one(
    state: Any, slug: str, member: str, slot_key: str, book: dict[str, dict[str, Any]]
) -> bool:
    """Wake one Bot when its fields are due; whether it was woken.

    The Bot's new row is saved BEFORE the turn starts, and *book* takes it only once
    the turn started: a wake the book does not hold would be repeated every tick.
    Raises :class:`BookUnwritable` when the row cannot be saved.
    """
    from kiro_crew import crew_pause, dashboard_agentic
    from kiro_crew.dashboard import goal_reminder
    from kiro_crew.dashboard.mate_welcome import (
        dispatch_hidden_turn,
        hidden_turn_language,
        with_language,
    )

    stale, line = await asyncio.to_thread(dashboard_agentic.member_stale_state, slug, member)
    now = time.time()
    fields = due_fields(book.get(slug), stale, now)
    if not fields or not line:
        return False
    if await goal_reminder._load_thread(state, slot_key):
        return False
    slot = state.get_slot(slot_key)
    if (
        slot is None
        or getattr(slot, "agent", "") != member
        or getattr(slot, "mode", "") != members_mod.DM_SLOT_MODE
        or getattr(slot, "executor", "") == "remote"
    ):
        return False
    if crew_pause.holds_slot(slot):
        return False
    if getattr(slot, "_queue", None):
        # A thread with a turn queued is skipped and tried again on the next tick:
        # booking it here would lose the wake if that queued turn is cancelled.
        return False
    if goal_reminder._monitor_loop_drives(slot_key):
        return False
    language = await asyncio.to_thread(hidden_turn_language)
    if goal_reminder.gateway_stopping():
        return False
    again, skip = goal_reminder._idle_thread(state, slot_key, member)
    if skip or again is not slot:
        return False
    row = woken_row(stale, now)
    if not await asyncio.to_thread(write_book, {**book, slug: row}):
        raise BookUnwritable(slug)
    # The save ran off the loop: a turn may have started meanwhile. No await
    # between this check and the dispatch.
    again, skip = goal_reminder._idle_thread(state, slot_key, member)
    started = False
    if not skip and again is slot and not goal_reminder.gateway_stopping():
        started = dispatch_hidden_turn(
            state,
            slot,
            with_language(wake_prompt(line), language, in_thread=True),
            human_seen=False,
        )
    if started is False:
        # Put the saved row back; if that fails too, this period goes unwoken,
        # which costs nothing.
        await asyncio.to_thread(write_book, book)
        return False
    book[slug] = row
    await asyncio.to_thread(_record, slug, member, fields)
    return True


def quiet_outcome(outcome: str) -> bool:
    """Whether a tick's outcome woke no one, so its run leaves no history row."""
    return outcome == "nothing to wake" or outcome.startswith("skipped:")


async def fire(state: Any, crons: Any = None) -> str:
    """One tick of the stale-wake job; returns a one-line outcome for the run log."""
    switch = await asyncio.to_thread(switch_state)
    if switch is None:
        # Torn or unreadable config: wake no one and leave the job in place.
        return "skipped: the config could not be read"
    if not switch:
        # The switch went off while the gateway was down: the job removes itself.
        try:
            await sync_job(crons)
        except Exception:
            logger.debug("dashboard stale wake: removing the job failed", exc_info=True)
        return "skipped: the preview is off"
    if state is None:
        return "skipped: the dashboard is not running"
    listed = await asyncio.to_thread(bots)
    # Only Bots in the crew keep a row, so the book stays the crew's size.
    present = {slug for slug, _member, _slot in listed}
    book = {
        slug: row for slug, row in (await asyncio.to_thread(read_book)).items() if slug in present
    }
    woken = 0
    for slug, member, slot_key in listed:
        try:
            if await _wake_one(state, slug, member, slot_key, book):
                woken += 1
        except BookUnwritable:
            # No wake may start while the book cannot be saved.
            return f"stopped: the wake book could not be saved (asked {woken} Bot(s))"
        except Exception:
            logger.warning("dashboard stale wake for %s failed", slug, exc_info=True)
    return f"asked {woken} Bot(s) to update" if woken else "nothing to wake"
