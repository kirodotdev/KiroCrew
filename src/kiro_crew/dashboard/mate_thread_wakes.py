"""A crewmate's worker reports back: its turn end wakes the crewmate's own thread.

``crew-mode.md``, "Conductor and worker". When a worker session the crewmate
created ends a turn, the gateway runs ONE hidden turn in the crewmate's pinned
thread (:func:`kiro_crew.dashboard.mate_welcome.dispatch_hidden_turn`) carrying a
bounded, scrubbed excerpt of the worker's reply as data (:func:`report_prompt`,
envelope ``[Worker report]``).

* **Only the crewmate's own workers**, linked in this process (``_lineage_minted``
  or ``_reports_to_creator``) or, after a restart or a revive, by the crew log's
  gateway-written session tree (:func:`_lineage_names`); transcript metadata
  alone wakes nobody. A
  crewmate's changed cron result reports the same way (:func:`note_cron_result`).
* **One wake per turn end, deferred while busy**: one pending entry per worker,
  delivered when the thread's own turn task ends, never on a timer, never twice.
  Pending reports live in memory.

The same flush delivers a moved crewmate's one upgrade notice first
(:mod:`kiro_crew.mate_upgrade_notice`). Per-gateway state lives on the dashboard
state object, never in module globals.
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


def _missing_capability_line(missing: str, manual: bool = False) -> str:
    """The line under a worker's gap: find it and raise the card, or ask the user.

    *manual* when the crewmate's Dynamic capabilities preview is off
    (``kiro_crew.crewmate_dynamic_capabilities``): its conductor has
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


def _lineage_names(slot_key: str, creator_key: str) -> bool:
    """Whether the crew log's session tree names *creator_key* as *slot_key*'s parent.

    The gateway writes that edge at the child's first turn from its in-process
    witness, never from transcript metadata, so it survives a restart and a revive
    where the in-memory witness does not. An unknown tree (crew log off, projection
    unseeded) answers False. No I/O (``session_tree._slot_tree_parent``).
    """
    from kiro_crew.dashboard.session_control_parts.session_tree import _slot_tree_parent

    try:
        known, parent, _ = _slot_tree_parent(slot_key)
    except Exception:  # noqa: BLE001 - an unreadable tree attests nothing
        return False
    return bool(known) and parent == creator_key


def reporting_creator(state: Any, slot: Any) -> Any:
    """The crewmate thread *slot*'s turn end reports to, else ``None``."""
    creator_key = getattr(slot, "_created_by", "") or ""
    if not isinstance(creator_key, str) or not creator_key:
        return None
    witnessed = getattr(slot, "_lineage_minted", False) or getattr(
        slot, "_reports_to_creator", False
    )
    if not witnessed and not _lineage_names(slot.key, creator_key):
        return None
    creator = state.get_slot(creator_key)
    if creator is None or creator is slot or not is_crewmate_thread(creator):
        return None
    return creator


def page_line(slug: str) -> str:
    """The line that sends a worker report's news to crewmate *slug*'s Dashboard page.

    A worker report is where a dispatched job's state changes, and the check-ins that
    otherwise ask for the page (``goal_reminder``) run only with a goal and a switch
    on. So the wake names the page's own fields: a page in force with fields the
    crewmate writes gets ``worker_report.dashboard_fields`` naming them; a tab still
    on the suggestions page gets ``dashboard_agentic.SUGGESTIONS_LINE``, the turn
    block's build-your-page line. No line with ``agent.crew_panel`` off (no page
    tools), under the person's lock while the tab shows suggestions (an apply would
    be refused), or for a page whose every field is folded. TOTAL: a page that
    cannot be read costs the line. Blocking IO.
    """
    if not slug:
        return ""
    try:
        from kiro_crew import dashboard_agentic
        from kiro_crew.dashboard_templates import instance as template_instance

        if not members_mod.crew_panel_enabled():
            return ""
        if dashboard_agentic.tab_shows_suggestions(slug):
            if template_instance.is_locked(slug):
                return ""
            return dashboard_agentic.SUGGESTIONS_LINE
        page = dashboard_agentic.page_in_force(slug)
        if page is None:
            return ""
        names = sorted(dashboard_agentic.agentic_fields(page[1]))
    except Exception:
        logger.debug("worker report for %s: page lookup failed", slug, exc_info=True)
        return ""
    if not names:
        return ""
    return crewmate_prompts.fill("worker_report.dashboard_fields", fields=", ".join(names))


def with_page(prompt: str, page: str) -> str:
    """*prompt* (:func:`report_prompt`) closed by *page* (:func:`page_line`), when any."""
    return f"{prompt}\n\n{page}" if page else prompt


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
        cut = cut or bool(report.get("cut"))
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


def _kept_notices(state: Any) -> dict[str, str]:
    """Threads whose claimed kept notice (its prompt) is still to be delivered."""
    kept = getattr(state, "_mate_kept_notices", None)
    if not isinstance(kept, dict):
        kept = {}
        state._mate_kept_notices = kept
    return kept


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
    from kiro_crew.mate_upgrade_notice import claim_kept_notice, claim_notice

    binding = await asyncio.to_thread(members_mod.read_dm_binding_for_slot, slot.key)
    slug = members_mod.slug_from_dm_slot_key(slot.key)
    if not binding or binding.get("member") != slot.agent or not slug:
        return False
    if await asyncio.to_thread(claim_notice, slug, slot.agent):
        _notices(state).add(slot.key)
    else:
        # Not moved: a crewmate the migration left where it was says why, once.
        kept = await asyncio.to_thread(claim_kept_notice, slug, slot.agent)
        if not kept:
            return False
        _kept_notices(state)[slot.key] = kept
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
        owed = key in _notices(state) or key in _kept_notices(state)
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
        if _idle(slot) and key in _kept_notices(state):
            dispatch_hidden_turn(state, slot, _kept_notices(state).pop(key), human_seen=False)
            logger.info("kept notice started on %s", key)
            continue
        # The Dashboard line reads the page store: off the loop, before the idle check.
        slug = (members_mod.slug_from_dm_slot_key(key) or "") if reports else ""
        page = await asyncio.to_thread(page_line, slug) if slug else ""
        # The crewmate's own Dynamic capabilities preview picks the gap line. It is
        # a config read, so it runs off the loop too, and only when a report names a
        # gap; the re-check below covers both reads.
        manual = False
        gap = False
        if reports:
            gap = any(missing_capability(_excerpt(r["reply"])[0]) for r in reports.values())
        if gap:
            from kiro_crew import crewmate_dynamic_capabilities

            manual = await asyncio.to_thread(
                crewmate_dynamic_capabilities.restricted_for, slot.agent
            )
        if (slug or gap) and (
            state.get_slot(key) is not slot
            or not is_crewmate_thread(slot)
            or getattr(slot, "is_closing", False)
        ):
            # The thread was closed or replaced during the read: no wake on it, and
            # what is pending stays for the thread's next turn end.
            return
        if not reports:
            return
        if _idle(slot):
            _pending(state).pop(key, None)
            dispatch_hidden_turn(
                state,
                slot,
                with_page(report_prompt(list(reports.values()), slot.agent, manual=manual), page),
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


def _record_report(state: Any, thread_key: str, key: str, title: str, reply: str) -> None:
    """Hold worker *key*'s latest reply for crewmate thread *thread_key* and schedule the wake.

    One entry per worker, replaced and counted by its later turn ends. The reply
    is held as its excerpt (at most :data:`MAX_REPORT_CHARS`) with a cut flag.
    """
    reports = _pending(state).setdefault(thread_key, {})
    prior = reports.get(key)
    text, cut = _excerpt(reply)
    reports[key] = {
        "key": key,
        "title": title,
        "reply": text,
        "cut": cut,
        "turns": (prior["turns"] + 1) if prior else 1,
    }
    schedule_flush(state, thread_key)


def _is_jobs_own_cron_key(job: Any, session_key: str) -> bool:
    """Whether *session_key* is a key ``build_cron_session_context`` mints for *job*."""
    stable = f"cron:{getattr(job, 'id', '')}"
    return bool(getattr(job, "id", "")) and (
        session_key == stable or session_key.startswith(stable + ":")
    )


async def note_cron_result(state: Any, job: Any, session_key: str, reply_text: str) -> bool:
    """Report a crewmate's scheduled turn to its thread, as one of its workers. Never raises.

    The caller decides the result CHANGED; this decides whose it is, from
    attested state only: *session_key* is *job*'s own ``cron:`` key, its capture
    names ``job.member_id``, and the turn ran on that crewmate's worker. Then it
    takes the worker path. Returns whether a report was recorded.
    """
    try:
        member = getattr(job, "member_id", "") or ""
        if not isinstance(member, str) or not member or not _is_jobs_own_cron_key(job, session_key):
            return False
        from kiro_crew.execution_context import execution_from_record
        from kiro_crew.mate_worker import cron_worker_execution, is_own_worker_execution

        record = getattr(job, "execution_context", None)
        if not isinstance(record, dict):
            return False
        execution = execution_from_record({"execution_context": record})
        if execution.member_id != member:
            return False
        # Either shape is that crewmate's worker turn: a job its worker created
        # captured the worker's own record, and one its chat (or 1.0) created
        # captured a member selection the fire runs on the worker.
        if not is_own_worker_execution(execution) and (
            await asyncio.to_thread(cron_worker_execution, execution) is None
        ):
            return False
        binding = await asyncio.to_thread(members_mod.read_dm_binding, member)
        thread_key = binding.get("slot_key") if isinstance(binding, dict) else ""
        thread = state.get_slot(thread_key) if thread_key else None
        if thread is None or not is_crewmate_thread(thread):
            return False
        title = crewmate_prompts.fill("worker_report.cron_title", name=getattr(job, "name", ""))
        _record_report(state, thread.key, session_key, title, reply_text or "")
        return True
    except Exception:
        logger.warning("cron report for %s failed", session_key, exc_info=True)
        return False


def note_turn_end(state: Any, slot: Any, reply_text: str | None) -> None:
    """Called once at the end of every top-level turn. Never raises.

    A worker's turn end records its report for its crewmate and schedules the
    wake; *reply_text* is ``None`` for a turn that ended before it reached the
    model, which reports nothing. A crewmate thread's own turn end schedules the
    delivery of anything still pending for it.
    """
    try:
        creator = reporting_creator(state, slot) if reply_text is not None else None
        if creator is not None:
            _record_report(
                state,
                creator.key,
                slot.key,
                slot.display_title,
                reply_text if isinstance(reply_text, str) else "",
            )
        elif is_crewmate_thread(slot):
            if (
                _pending(state).get(slot.key)
                or slot.key in _notices(state)
                or slot.key in _kept_notices(state)
            ):
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
