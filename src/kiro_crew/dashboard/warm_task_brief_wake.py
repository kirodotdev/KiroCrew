"""The warm brief's wake turn (RFC ``rfc-delayed-task-brief.md`` sections 3.8
and 3.8.1).

Before the one-call brief, a qualifying return runs one read-only turn of the
thread's crewmate in a fresh session: it may read files, deployments and
tickets with its own tools, and writes the same JSON the one-call brief does,
plus ``checks``. A check is kept only when every tool call it cites ran to
completion in this turn; the gateway records those tool-call ids as its
evidence and gives it a fact id, so a file, deploy or ticket line citing it
may stay. The turn is report-only: every call the read-only classifier cannot
prove is refused, so it closes no task and writes no ledger. On a timeout, a
failure, a cap or the owner typing, the one-call brief runs instead; nothing
is retried.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard import warm_task_brief_facts as facts

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

WARM_TASK_BRIEF_WAKE_TIMEOUT_SECS = 180.0
WARM_TASK_BRIEF_WAKE_MAX_TOOL_CALLS = 15
# Wakes per chat per local day (M5); past it the one-call brief runs.
WARM_TASK_BRIEF_WAKE_MAX_PER_THREAD_PER_DAY = 6
MAX_CHECKS = 8
_CHECK_KINDS = frozenset({"file", "deploy", "ticket"})
_CHECK_IDS = frozenset(f"c{i}" for i in range(1, MAX_CHECKS + 1))
_MAX_CHECK_CHARS = 160
_MAX_TRACKED = 1024

# Per chat, the local days of its wakes so far (in memory, loop thread only).
_wake_days: dict[str, list[str]] = {}
# Per chat with a wake running, the event that stops it.
_stops: dict[str, asyncio.Event] = {}
# Cancelled wake turns still tearing down their session.
_reaping: set[asyncio.Future[Any]] = set()


@dataclass
class Calls:
    """The wake's tool calls: ids in call order (``t1``, ``t2``, … to the
    model) and the ids whose result completed."""

    order: list[str] = field(default_factory=list)
    done: set[str] = field(default_factory=set)


def _reaped(turn: asyncio.Future[Any]) -> None:
    _reaping.discard(turn)
    if not turn.cancelled() and turn.exception() is not None:
        logger.debug("warm brief wake teardown failed", exc_info=turn.exception())


def stop(slot_key: str) -> bool:
    """Stop *slot_key*'s running wake (the owner typed); ``False`` when none runs."""
    event = _stops.get(slot_key)
    if event is not None:
        event.set()
    return event is not None


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _under_cap(slot_key: str, now: float) -> bool:
    return (
        _wake_days.get(slot_key, []).count(_day(now)) < WARM_TASK_BRIEF_WAKE_MAX_PER_THREAD_PER_DAY
    )


def _count(slot_key: str, now: float) -> None:
    days = [d for d in _wake_days.pop(slot_key, []) if d == _day(now)]
    _wake_days[slot_key] = [*days, _day(now)]
    while len(_wake_days) > _MAX_TRACKED:
        _wake_days.pop(next(iter(_wake_days)))


_WAKE_RULES = """\
You may use read-only tools to check open items whose acceptance names a
file, a deployment or a ticket. Every write is refused. Use at most {calls}
tool calls. Your tool calls are numbered t1, t2, ... in the order you make
them. Report each thing you checked in "checks", at most {checks}, ids c1 to
c{checks}: {{"id": "c1", "kind": "file" | "deploy" | "ticket", "label":
"<what you checked>", "result": "<what you found>", "tool_calls": ["t1"]}}.
A check whose tool calls did not complete is dropped. End with the JSON
object only."""

# Each sentence of the one-call prompt the wake rewords, and its wake form.
WAKE_EDITS = (
    (
        "The untrusted snapshot below is your only source.",
        "The untrusted snapshot below and your read-only tool results are your only sources.",
    ),
    (
        "Do not use tools.",
        _WAKE_RULES.format(calls=WARM_TASK_BRIEF_WAKE_MAX_TOOL_CALLS, checks=MAX_CHECKS),
    ),
    (
        "file, deploy and ticket lines are deleted (nothing checks them yet).",
        "a file, deploy or ticket line lists a check id (c1, ...) of that kind, or it is deleted.",
    ),
    (
        ' "body": "<pictures only, or empty>"}',
        ' "checks": [<your checks>],\n "body": "<pictures only, or empty>"}',
    ),
)


def wake_prompt(prompt: str) -> str:
    """The one-call *prompt* with the wake's rules in place of its tool ban."""
    for old, new in WAKE_EDITS:
        prompt = prompt.replace(old, new)
    return prompt


def session_key(slot_key: str) -> str:
    """The wake's own session: a ``thread:`` key, so governance and the audit
    log read it as the dashboard and the provider factory starts it on the
    configured default harness, like a reply thread's turn."""
    return f"thread:{slot_key}:warm-wake"


def _backend(cfg: Any, slot: "_ChatSlot") -> str | None:
    """The harness *slot*'s own chat runs on (a crewmate's pin, a member DM's
    route, else the default); ``None`` when it cannot tell. Reads agent
    files: call off the loop."""
    from kiro_crew.crewmate_harness import crewmate_backend_pin
    from kiro_crew.members import select_provider_backend

    try:
        agent, key = cfg.agent, f"dashboard:{slot.key}"
        return select_provider_backend(
            key,
            agent.member_acp_backend,
            agent.acp_backend,
            crew_backend=crewmate_backend_pin(cfg, key),
        )
    except Exception:
        logger.debug("warm brief wake: no backend for %s", key, exc_info=True)
        return None


def _may_wake(state: "DashboardState", cfg: Any, backend: str | None) -> bool:
    """Only when the crewmate's own harness is the one the wake session starts
    on: a pinned or re-routed crewmate never runs on another harness."""
    from kiro_crew.acp_backends import ACP_BACKENDS_SIDE_READONLY

    builder = getattr(state, "context_builder", None)
    return (
        getattr(builder, "hooks", None) is not None
        and callable(getattr(state, "run_background_turn", None))
        and backend in ACP_BACKENDS_SIDE_READONLY
        and backend == getattr(getattr(cfg, "agent", None), "acp_backend", None)
    )


async def _turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    cfg: Any,
    backend: str,
    prompt: str,
    calls: Calls,
) -> str:
    """One read-only turn in its own session, destroyed after: the shape of a
    thread reply's turn (``chat_threads``), every unproven call refused."""
    from kiro_crew.acp_backends import ACP_BACKEND_GOOSE, ACP_BACKEND_STRANDS
    from kiro_crew.agent_discovery import warm_project_agent_names
    from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name
    from kiro_crew.agent_sdk.context import CONTEXT_EVENT_TOOL as EVENT_TOOL_CALL
    from kiro_crew.agent_sdk.context import CONTEXT_EVENT_TOOL_RESULT as EVENT_TOOL_RESULT
    from kiro_crew.agent_sdk.drivers.acp import (
        claude_adapter_honours_session_options,
        goose_side_home_supported,
    )
    from kiro_crew.config.loader import resolve_agent_bindings
    from kiro_crew.dashboard.side_readonly_spec import publish_readonly_spec
    from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
    from kiro_crew.start_priority import StartPriority

    project = slot.project or None
    await warm_project_agent_names(project or "", operation="warm_brief_wake", source="dashboard")
    bindings = resolve_agent_bindings(cfg, slot.agent or None, project, validate_memory_files=False)
    if slot.agent and not slot._app and not bindings.requested_resolved:
        return ""  # the crewmate is gone: never answer as another agent
    agent = bindings.kiro_agent or slot.agent or None
    session: dict[str, Any] = {}
    if is_claude_backend_name(backend) or backend in (ACP_BACKEND_GOOSE, ACP_BACKEND_STRANDS):
        if is_claude_backend_name(backend) and not await asyncio.to_thread(
            claude_adapter_honours_session_options
        ):
            return ""
        if backend == ACP_BACKEND_GOOSE and not goose_side_home_supported():
            return ""
        session["side_read_only"] = True
    else:
        spec = await asyncio.to_thread(publish_readonly_spec, agent or "kirocrew", project)
        agent = spec.name

    async def observe(event: Any) -> None:
        call_id = event.tool_call_id
        if event.kind == EVENT_TOOL_CALL and call_id and call_id not in calls.order:
            calls.order.append(call_id)
        elif event.kind == EVENT_TOOL_RESULT and event.tool_final and call_id:
            calls.done.add(call_id)

    key = session_key(slot.key)
    provider, _new, _resumed = await state.sessions.get_or_create(
        key, agent=agent, cwd=project, start_priority=StartPriority.BACKGROUND, **session
    )
    try:
        return await stream_and_collect(
            provider,
            prompt,
            approval_policy=ToolApprovalPolicy.READ_ONLY,
            hooks=getattr(state.context_builder, "hooks", None),
            max_turns=WARM_TASK_BRIEF_WAKE_MAX_TOOL_CALLS,
            session_key=key,
            agent=slot.agent or "kirocrew",
            app=slot._app or "",
            unattended=True,
            retry_transient=False,  # a failed wake falls back to the one-call brief at once
            allow_image=False,
            on_event=observe,
        )
    finally:
        state.sessions.release(key)
        await state.sessions.destroy(key)


async def run(
    state: "DashboardState", slot: "_ChatSlot", cfg: Any, prompt: str, calls: Calls | None
) -> str | None:
    """The wake's JSON text; "" when no wake ran or it gave no brief (the
    caller then makes the one-call brief); ``None`` when the owner typed or
    left during it (no brief would post, so none is made). *calls* is filled
    while it runs and emptied on any failure, so a failed wake backs no check."""
    from kiro_crew.dashboard import warm_task_brief as brief_mod

    now = time.time()
    if calls is None or slot.key in _stops:
        return ""
    default = getattr(getattr(cfg, "agent", None), "acp_backend", None)
    if not _may_wake(state, cfg, default):  # the cheap gates first, before any file read
        return ""
    backend = await asyncio.to_thread(_backend, cfg, slot)
    if slot.key in getattr(state, "_warm_task_brief_abandoned", ()):
        return None  # the owner typed or left while the harness was resolved
    if backend is None or not _may_wake(state, cfg, backend):
        return ""
    if not _under_cap(slot.key, now):
        logger.info("warm brief wake skipped for %s: daily cap", slot.key)
        return ""
    _count(slot.key, now)
    stopped = _stops[slot.key] = asyncio.Event()
    turn = asyncio.ensure_future(
        state.run_background_turn(
            slot, _turn(state, slot, cfg, backend, wake_prompt(prompt), calls)
        )
    )
    stopper = asyncio.ensure_future(stopped.wait())
    text, why = "", "timeout"
    try:
        await asyncio.wait(
            {turn, stopper},
            timeout=WARM_TASK_BRIEF_WAKE_TIMEOUT_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stopped.is_set():
            why = "stopped"
        elif turn.done():
            why = "failed" if turn.cancelled() or turn.exception() else "no_brief"
            if why == "no_brief" and brief_mod._parse_result(turn.result()) is not None:
                text = turn.result()
    finally:
        _stops.pop(slot.key, None)
        stopper.cancel()
        if not turn.done():
            turn.cancel()
            _reaping.add(turn)
            turn.add_done_callback(_reaped)
        elif not turn.cancelled() and turn.exception() is not None:
            logger.debug("warm brief wake failed for %s", slot.key, exc_info=turn.exception())
        if not text:
            calls.order.clear()
            calls.done.clear()
    if not text:
        logger.info("warm brief wake for %s fell back: %s", slot.key, why)
    return None if why == "stopped" else text


def words(checked: Any) -> list[str]:
    """The words a wake check carries: model text, so the gateway's PR, SHA
    and time scans read them like every other model field."""
    return [
        str(text)
        for fact in checked
        if fact.get("source") == "wake"
        for text in (fact.get("label"), (fact.get("values") or {}).get("result"))
        if text
    ]


def with_checks(
    brief: Any, text: str, calls: Calls | None, base: list[dict[str, Any]]
) -> tuple[Any, list[dict[str, Any]]]:
    """*brief* and the fact table with the wake's proven checks added.

    A check stays when its id is new, its kind is file, deploy or ticket, and
    every tool call it cites (``tN``) completed. It takes the next ``fN`` and
    keeps the cited calls' real ids as ``evidence``; each line's ``cN`` is
    renamed to match. A line citing an id outside the table keeps nothing it
    could match later."""
    if calls is None or not calls.done:
        return brief, base
    from kiro_crew.dashboard import warm_task_brief as brief_mod

    raw = brief_mod._extract_json_of_type(
        text, dict, prefer=lambda value: isinstance(value, dict) and "checks" in value
    )
    entries = raw.get("checks") if isinstance(raw, dict) else None
    numbered = {f"t{n}": call_id for n, call_id in enumerate(calls.order, 1)}
    table, renamed = list(base), {}
    for entry in entries[:MAX_CHECKS] if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        check_id, kind, cited = entry.get("id"), entry.get("kind"), entry.get("tool_calls")
        label = brief_mod._clean(entry.get("label"), _MAX_CHECK_CHARS)
        result = brief_mod._clean(entry.get("result"), _MAX_CHECK_CHARS)
        ids = [numbered.get(str(ref), "") for ref in cited] if isinstance(cited, list) else []
        if (
            not isinstance(check_id, str)
            or check_id not in _CHECK_IDS
            or check_id in renamed
            or not (isinstance(kind, str) and kind in _CHECK_KINDS)
            or not (label and result and ids)
            or not all(call_id in calls.done for call_id in ids)
            or len(table) >= facts.MAX_FACTS
        ):
            continue
        renamed[check_id] = fact_id = f"f{len(table) + 1}"
        evidence = list(dict.fromkeys(ids))
        table.append(
            {"id": fact_id, "kind": kind, "label": label, "values": {"result": result},
             "source": "wake", "evidence": evidence}
        )  # fmt: skip
    known = {fact["id"] for fact in base}
    lines = tuple(
        {**line, "facts": [renamed.get(ref, ref if ref in known else "-") for ref in line["facts"]]}
        for line in brief.lines
    )
    return replace(brief, lines=lines), table
