"""A crewmate's worker reports back: its turn end wakes the crewmate's own thread.

A crewmate's chat only dispatches (``crew-mode.md``, "Conductor and worker"): it
opens a worker session with ``session_create``, briefs it with ``session_send``
and ends its turn, so it is free for the user at any moment. The worker's answer
comes back on its own: when a worker session the crewmate created ends a turn,
the gateway runs ONE hidden turn in the crewmate's pinned thread
(:func:`kiro_crew.dashboard.mate_welcome.dispatch_hidden_turn`, actor
``gateway``, no user row) carrying a bounded, scrubbed excerpt of the worker's
latest reply, framed as the worker's data rather than an instruction
(:func:`report_prompt`, envelope ``[Worker report]``).

* **Only the crewmate's own workers.** The worker's ``_created_by`` must name a
  live crewmate thread (a member DM slot, not the reserved ``default`` member,
  not remote), and this process must have witnessed the link: the child was
  minted here by ``session_create`` (``_lineage_minted``), or its creator sent
  it a message here (``_reports_to_creator``, stamped by ``session_send``). A
  ``_created_by`` read back from transcript metadata alone wakes nobody.
* **One wake per turn end, deferred while busy.** An idle thread is woken at
  once. A thread with a turn running or a message queued keeps the report
  pending, one entry per worker (a later turn end of the same worker replaces
  it and counts the turns), and is woken when that turn ends: the flush waits on
  the thread's own turn task, never on a timer, and every turn end of the
  thread schedules it again, so a pending report is delivered once the thread
  is idle and never twice. Pending reports live in memory; the upgrade's
  restart drops them along with the turns that produced them.

The same deferred flush delivers the one upgrade notice a crewmate the
migration moved owes its user (:mod:`kiro_crew.mate_upgrade_notice`), ahead of
any report: offered to every open crewmate thread after the slot restores
(:func:`offer_upgrade_notices`) and to any other at its first turn end.

Per-gateway state lives on the dashboard state object (``_mate_wakes``,
``_mate_wake_tasks``, ``_mate_notices``, ``_mate_notice_checked``), never in
module globals.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from kiro_crew import crewmate_prompts
from kiro_crew import members as members_mod

logger = logging.getLogger(__name__)

#: The envelope a worker report opens with.
WORKER_REPORT_PREFIX = "[Worker report]"

#: How much of a worker's reply one report carries.
MAX_REPORT_CHARS = 4000

_BEGIN = "<<<worker reply"
_END = ">>>end of worker reply"

_PREAMBLE = crewmate_prompts.text("wake.preamble")

_GUIDANCE = crewmate_prompts.text("worker_report.guidance")


#: Said under a report whose worker names a capability it lacks
#: (``mate_worker.MISSING_CAPABILITY_PREFIX``).
_MISSING_CAPABILITY_NOTE = crewmate_prompts.text("worker_report.missing_capability")
_MISSING_CAPABILITY_CHARS = 240
_LINE_DECORATION = " \t*_`#>-"


def _missing_capability_line(missing: str, manual: bool) -> str:
    """The line under a worker's gap: find it and raise the card, or ask the user.

    *manual* when the crewmate's Dynamic capabilities preview is off under
    Crewmate 2.0 (``kiro_crew.crewmate_dynamic_capabilities``): its conductor has
    neither find_capabilities nor request_capabilities, so it is told to ask the
    user to add the capability in its advanced settings, in one plain line, no card.
    """
    if manual:
        return crewmate_prompts.fill("worker_report.missing_capability_manual", missing=missing)
    return crewmate_prompts.fill("worker_report.missing_capability", missing=missing)


def missing_capability(text: str) -> str:
    """What a worker's reply says it was missing, from its ``MISSING CAPABILITY:`` line, else ``""``.

    The line may carry Markdown emphasis or a quote mark; the text after the
    prefix is cut to a bounded length. *text* is the already scrubbed excerpt.
    """
    from kiro_crew.mate_worker import MISSING_CAPABILITY_PREFIX

    prefix = MISSING_CAPABILITY_PREFIX.casefold()
    for line in text.splitlines():
        bare = line.strip(_LINE_DECORATION)
        if bare.casefold().startswith(prefix):
            what = bare[len(prefix) :].strip(_LINE_DECORATION)
            return " ".join(what.split())[:_MISSING_CAPABILITY_CHARS] or "(not named)"
    return _blocked_for_a_tool(text)


#: A step the worker says it could not do...
_BLOCKED = re.compile(r"\b(?:blocked|cannot|can't|can not|unable|could not|couldn't)\b", re.I)
#: ...for want of a named tool or server: "no issuetracker tool", "without the jira server".
_LACKED_TOOL = re.compile(
    r"\b(?:no|without|lacks?|lacking|missing)\s+(?:an?\s+|the\s+)?([A-Za-z][\w.-]{1,40})\s+"
    r"(?:tool|tools|server|mcp server)\b",
    re.I,
)


def _blocked_for_a_tool(text: str) -> str:
    """The gap a worker states in prose instead of the ``MISSING CAPABILITY:`` line.

    Round-5 Cove: "Step 2: blocked on issuetracker (no issuetracker tool)". Narrow on
    purpose: a line that says a step is blocked (or could not be done) AND names the
    tool or server it lacked. ``"the issuetracker tool"`` for that line, else ``""``.
    """
    for line in text.splitlines():
        if not _BLOCKED.search(line):
            continue
        lacked = _LACKED_TOOL.search(line)
        if lacked:
            noun = lacked.group(0).split()[-1].lower()
            return f"the {lacked.group(1)} {noun} (in the worker's words: {line.strip(_LINE_DECORATION)})"[
                :_MISSING_CAPABILITY_CHARS
            ]
    return ""


def is_crewmate_thread(slot: Any) -> bool:
    """Whether *slot* is a crewmate's own pinned thread on this gateway."""
    agent = getattr(slot, "agent", "")
    return (
        slot is not None
        and getattr(slot, "mode", "") == members_mod.DM_SLOT_MODE
        and isinstance(agent, str)
        and bool(agent)
        and agent != "default"
        and getattr(slot, "executor", "") != "remote"
    )


def reporting_creator(state: Any, slot: Any) -> Any:
    """The crewmate thread *slot*'s turn end reports to, else ``None``."""
    creator_key = getattr(slot, "_created_by", "") or ""
    if not isinstance(creator_key, str) or not creator_key:
        return None
    if not (getattr(slot, "_lineage_minted", False) or getattr(slot, "_reports_to_creator", False)):
        return None
    creator = state.get_slot(creator_key)
    if creator is None or creator is slot or not is_crewmate_thread(creator):
        return None
    return creator


def _excerpt(text: str) -> tuple[str, bool]:
    from kiro_crew.member_goals import scrub_text

    clean = scrub_text(text).strip().replace(_END, "")
    if len(clean) <= MAX_REPORT_CHARS:
        return clean, False
    return clean[:MAX_REPORT_CHARS].rstrip(), True


def report_prompt(
    reports: list[dict[str, Any]], member: str = "", *, manual: bool | None = None
) -> str:
    """The hidden turn that hands crewmate *member* its workers' latest replies.

    *manual* is *member*'s Dynamic capabilities preview being off
    (``crewmate_dynamic_capabilities.restricted_for``), which picks the gap line;
    when not given it is read here (a config read), and no crewmate named reads
    as off. The wake loop reads it off the event loop and passes it in.
    """
    if manual is None:
        from kiro_crew import crewmate_dynamic_capabilities

        manual = crewmate_dynamic_capabilities.restricted_for(member)
    lines = [WORKER_REPORT_PREFIX + " " + _PREAMBLE.rstrip(), ""]
    for report in reports:
        turns = report["turns"]
        ended = (
            crewmate_prompts.text("worker_report.ended_one")
            if turns == 1
            else crewmate_prompts.fill("worker_report.ended_many", turns=turns)
        )
        lines.append(
            crewmate_prompts.fill(
                "worker_report.session", title=report["title"], key=report["key"], ended=ended
            )
        )
        text, cut = _excerpt(report["reply"])
        if not text:
            lines.append(crewmate_prompts.text("worker_report.no_reply"))
        else:
            lines.append(
                crewmate_prompts.text("worker_report.latest") + (", cut short:" if cut else ":")
            )
            lines.extend([_BEGIN, text, _END])
            missing = missing_capability(text)
            if missing:
                lines.append(_missing_capability_line(missing, manual))
        lines.append("")
    lines.append(_GUIDANCE)
    return "\n".join(lines)


def _pending(state: Any) -> dict[str, dict[str, dict[str, Any]]]:
    pending = getattr(state, "_mate_wakes", None)
    if not isinstance(pending, dict):
        pending = {}
        state._mate_wakes = pending
    return pending


def _tasks(state: Any) -> dict[str, asyncio.Task[Any]]:
    tasks = getattr(state, "_mate_wake_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        state._mate_wake_tasks = tasks
    return tasks


def _notices(state: Any) -> set[str]:
    """Threads whose claimed upgrade notice is still to be delivered."""
    notices = getattr(state, "_mate_notices", None)
    if not isinstance(notices, set):
        notices = set()
        state._mate_notices = notices
    return notices


def _notice_checked(state: Any) -> set[str]:
    checked = getattr(state, "_mate_notice_checked", None)
    if not isinstance(checked, set):
        checked = set()
        state._mate_notice_checked = checked
    return checked


async def offer_upgrade_notice(state: Any, slot: Any) -> bool:
    """Queue crewmate thread *slot*'s upgrade notice when it owes one. Once per thread.

    The notice is claimed on disk before it is queued, so it is posted at most
    once ever; a restart between the claim and the delivery loses it, which is
    the safe direction.
    """
    if not is_crewmate_thread(slot) or slot.key in _notice_checked(state):
        return False
    _notice_checked(state).add(slot.key)
    from kiro_crew.mate_upgrade_notice import claim_notice

    binding = await asyncio.to_thread(members_mod.read_dm_binding_for_slot, slot.key)
    slug = members_mod.slug_from_dm_slot_key(slot.key)
    if not binding or binding.get("member") != slot.agent or not slug:
        return False
    if not await asyncio.to_thread(claim_notice, slug, slot.agent):
        return False
    _notices(state).add(slot.key)
    schedule_flush(state, slot.key)
    return True


async def offer_upgrade_notices(state: Any) -> None:
    """Offer every open crewmate thread its upgrade notice. Never raises."""
    for slot in list(getattr(state, "_slots", {}).values()):
        try:
            await offer_upgrade_notice(state, slot)
        except Exception:
            logger.warning(
                "upgrade notice check failed for %s", getattr(slot, "key", "?"), exc_info=True
            )


def _idle(slot: Any) -> bool:
    return not slot.running and not getattr(slot, "_queue", None)


async def _flush(state: Any, key: str) -> None:
    """Deliver *key*'s pending reports as one hidden turn once the thread is idle."""
    from kiro_crew.dashboard.mate_welcome import app_is_stopping, dispatch_hidden_turn

    while True:
        slot = state.get_slot(key)
        reports = _pending(state).get(key)
        owed = key in _notices(state)
        if not (reports or owed) or slot is None or not is_crewmate_thread(slot):
            # Nothing to say, or the thread is gone: what is pending stays, and
            # the thread's next turn end schedules this again.
            return
        if app_is_stopping(state):
            # A restart cuts the worker whose turn end brought us here; a wake
            # started now would be cut too and leave the crewmate's thread
            # reading as interrupted. Nothing is popped.
            return
        if _idle(slot) and key in _notices(state):
            # The upgrade notice goes first, as its own turn; the loop then
            # waits on that turn before any report follows.
            from kiro_crew.mate_upgrade_notice import NOTICE_PROMPT

            _notices(state).discard(key)
            dispatch_hidden_turn(state, slot, NOTICE_PROMPT, human_seen=False)
            logger.info("upgrade notice started on %s", key)
            continue
        if not reports:
            return
        # The crewmate's own Dynamic capabilities preview picks the gap line; read
        # off the loop before the idle check, so the dispatch below stays in step.
        from kiro_crew import crewmate_dynamic_capabilities

        manual = await asyncio.to_thread(crewmate_dynamic_capabilities.restricted_for, slot.agent)
        if _idle(slot):
            _pending(state).pop(key, None)
            dispatch_hidden_turn(
                state,
                slot,
                report_prompt(list(reports.values()), slot.agent, manual=manual),
                human_seen=False,
            )
            logger.info("worker report woke %s (%d worker(s))", key, len(reports))
            return
        task = getattr(slot, "task", None)
        if not isinstance(task, asyncio.Future) or task.done():
            return
        await asyncio.wait({task})


def _keep(state: Any, task: asyncio.Task[Any]) -> None:
    """Hold *task* in the state's background set so it is not collected mid-flight."""
    tasks = getattr(state, "_background_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)


def schedule_flush(state: Any, key: str) -> None:
    """Start *key*'s flush unless one is already waiting on its thread."""
    tasks = _tasks(state)
    running = tasks.get(key)
    if running is not None and not running.done():
        return

    async def run() -> None:
        try:
            await _flush(state, key)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("worker report for %s failed", key, exc_info=True)
        finally:
            if tasks.get(key) is task:
                tasks.pop(key, None)

    task = asyncio.get_running_loop().create_task(run())
    tasks[key] = task


def note_turn_end(state: Any, slot: Any, reply_text: str | None) -> None:
    """Called once at the end of every top-level turn. Never raises.

    A worker's turn end records its report for its crewmate and schedules the
    wake; *reply_text* is ``None`` for a turn that ended before it reached the
    model, which reports nothing. A crewmate thread's own turn end schedules the
    delivery of anything still pending for it. Nothing at all while Crewmate 2.0
    is off (``kiro_crew.crewmate_v2``): no conductor hands work to a worker then.
    """
    from kiro_crew import crewmate_v2

    if not crewmate_v2.enabled():
        return
    try:
        creator = reporting_creator(state, slot) if reply_text is not None else None
        if creator is not None:
            reports = _pending(state).setdefault(creator.key, {})
            prior = reports.get(slot.key)
            reports[slot.key] = {
                "key": slot.key,
                "title": slot.display_title,
                "reply": reply_text if isinstance(reply_text, str) else "",
                "turns": (prior["turns"] + 1) if prior else 1,
            }
            schedule_flush(state, creator.key)
        elif is_crewmate_thread(slot):
            if _pending(state).get(slot.key) or slot.key in _notices(state):
                schedule_flush(state, slot.key)
            if slot.key not in _notice_checked(state):
                # A thread that was not open at startup is offered its notice here.
                _keep(
                    state, asyncio.get_running_loop().create_task(offer_upgrade_notice(state, slot))
                )
    except Exception:
        logger.warning(
            "worker report bookkeeping failed for %s", getattr(slot, "key", "?"), exc_info=True
        )
