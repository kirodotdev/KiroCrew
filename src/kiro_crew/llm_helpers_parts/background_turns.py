"""Unattended turns: the background one-liner and the shared background turn.

``run_bg_oneliner`` (titles, summaries and other one-shot prompts on the shared
background session) and ``background_turn`` (a scoped turn on a recyclable session),
with the crew-log owner each pins, its usage row, and the memory-consolidation
session cleanup.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from kiro_crew.agent_sdk.drivers.acp_turn_vocab import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    AcpError,
)
from kiro_crew.constants import DENY_CAUSE_SURFACE_POLICY
from kiro_crew.image_refs import strip_image_refs
from kiro_crew.llm_helpers_parts.backend_errors import is_context_overflow_error
from kiro_crew.llm_helpers_parts.model_fallback import first_advertised_fallback
from kiro_crew.llm_helpers_parts.tool_approval import _steer_host_deny
from kiro_crew.llm_helpers_parts.turn_usage import (
    _billing_stats,
    _provider_label,
    provider_last_turn_usage,
    usage_has_billing,
)
from kiro_crew.metrics.usage_attrs import TRIGGER_SYSTEM
from kiro_crew.sel import sel as _sel
from kiro_crew.start_priority import StartPriority

logger = logging.getLogger("kiro_crew.llm_helpers")


def _raise_if_unfinished(event: object, text: str) -> None:
    """Raise when the backend never finished the turn, so its text is cut off.

    The session handle ends a turn its backend did not finish within the turn
    ceiling with ``STOP_REASON_TIMEOUT``, and one its stale watchdog gave up on with
    ``STOP_REASON_STALE_RECOVER``. Both arrive as an ordinary ``EVENT_COMPLETE``, so
    a one-liner would return the partial text as its answer. Raising runs the
    caller's failure path instead. The error is the prompt-timeout error, carrying
    the partial text, and it is not transient: a retry would wait out the same
    stalled backend again.
    """
    from kiro_crew.agent_sdk.drivers.acp_vocab import (
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TIMEOUT,
        AcpTimeoutError,
    )

    stop_reason = str(getattr(event, "stop_reason", "") or "")
    if stop_reason not in (STOP_REASON_TIMEOUT, STOP_REASON_STALE_RECOVER):
        return
    error = AcpTimeoutError(
        text,
        message=f"background one-liner: the backend did not finish the turn ({stop_reason})",
    )
    error.transient = False
    raise error


async def _refuse_while_memory_critical(what: str) -> None:
    """Raise instead of starting background helper work while host memory is critical.

    The verdict is ``resource_status.admission_check``, the gate cron firings and
    task-runner steps already defer on, so it fails open and
    ``agent.admission_gate: false`` turns it off. It raises before any session is
    acquired, where a failed acquisition already raises: every caller's existing
    failure path handles it, with nothing started and nothing billed.
    """
    # Function-local: a module-level name here would join the facade's served surface.
    from kiro_crew.resource_status import admission_check

    decision = await asyncio.to_thread(admission_check)
    if not decision.admitted:
        logger.info("Background helper %s not started: %s", what, decision.reason)
        raise RuntimeError(f"background helper {what!r} not started: {decision.reason}")


async def run_bg_oneliner(
    sessions: Any,
    prompt: str,
    *,
    model: str | None = None,
    sel_source: str = "bg_oneliner",
    sel_session_key: str = "_bg",
    timeout: float | None = None,
    strict_model: bool = False,
    crew_log_kind: str = "",
    crew_log_session_key: str = "",
    max_output_bytes: int | None = None,
    retry_rejected_model: bool = True,
    start_priority: StartPriority = StartPriority.BACKGROUND,
    memory_gate: bool = True,
) -> str:
    """Stream a single prompt through an ephemeral background session and return
    the accumulated text.

    ``start_priority`` orders the session's start; FOREGROUND only for a caller a
    person is waiting on (rule: ``kiro_crew.start_priority``). A BACKGROUND call
    raises without starting while host memory is critical
    (:func:`_refuse_while_memory_critical`), unless ``memory_gate=False``: for a
    caller whose failure path costs more than the call, as a refused nudge-wake
    judge fires the full turn it exists to suppress. ``timeout`` bounds
    the DRIVE only -- see the acquisition below for why it must not be cancelled.

    ``strict_model=True`` makes the requested ``model`` a hard requirement
    rather than a preference: a failed ``set_model`` override raises instead
    of silently degrading to the session default, and the reactive
    rejected-model fallback below is disabled. Callers whose RESULT is only
    meaningful on that exact model (e.g. the poisoned-conversation canary in
    chat_runner, which uses a success as evidence to discard a conversation
    served by that same model) must set it; ordinary best-effort callers
    (title/nav/folder generation) keep the default lenient behavior.

    Acquires a ``_bg`` session, best-effort pins the cheap model, drives the
    event loop and ``destroy()``s the session in ``finally``. The task is
    tool-free by contract: permission requests are rejected and **always**
    SEL-logged as ``denied`` (``backend-security-controls``), under
    ``sel_source`` or the default ``"bg_oneliner"``.

    ``crew_log_kind`` and ``crew_log_session_key`` are BOTH required to write to
    that session's crew log. Most callers (a tip, a folder icon, a cron label)
    are shared work, so the default writes nothing rather than charge whichever
    session triggered it.

    Errors propagate; callers that want a best-effort "" wrap the call. ``sessions``
    is duck-typed (anything with ``get_bg_session()``) to avoid a
    dashboard/session import cycle.

    ``max_output_bytes`` bounds accumulated UTF-8 text before concatenation;
    ``retry_rejected_model=False`` disables the extra reactive model call for a
    caller that accounts each attempt against a hard call budget. Neither changes
    the configured-model resolution or the default behavior of other callers.
    """
    # Pinned before the acquisition, which is itself a suspension point: a slot
    # reset, switch or compaction meanwhile gives the successor a new ACP session
    # id. The pin says whether the slot moved during the call (the entry then goes to
    # the newest unit, since an older unit never grows).
    _crew_log_owner = _background_crew_log_owner(sessions, crew_log_session_key, crew_log_kind)
    if memory_gate and start_priority is StartPriority.BACKGROUND:
        await _refuse_while_memory_critical(sel_source)
    # NOT under ``timeout``: the acquisition can be the shared ``_bg`` runtime's own
    # (re)spawn, run inline under its lock, and a cancelled one is killed with
    # nothing assigned -- so a short-budget caller would destroy the runtime every
    # later caller reuses, repeatedly. Cancelling it after ``session/new`` went out
    # also leaks that session: the gate's late-answer collector belongs to the
    # runtime's own timeout, not to a caller's. ``start_priority`` is what keeps a
    # person's wait here short; the queue waits are bounded by the gate's own
    # budgets.
    session = await sessions.get_bg_session(start_priority=start_priority)
    # The stats object as it stands BEFORE this turn. The runner replaces it when
    # a turn actually begins, so comparing identity at teardown separates a turn
    # that ran from one whose dispatch failed while the previous turn's already
    # recorded credits were still installed.
    stats_before = _billing_stats(session)
    # Wall clock for the turn itself, started after the session is in hand so the
    # acquire wait is not charged as turn time. The acp provider never fills
    # TurnUsage.duration_ms, so this local measurement is the only duration a
    # background row can carry, and every other dispatch surface supplies one.
    turn_started = time.monotonic()

    async def _drive(model_to_use: str | None) -> str:
        text = ""
        output_bytes = 0
        set_model = getattr(session, "set_model", None)
        # set_model resolves the preference (often "auto") against the advertised
        # list (AcpSessionHandle.set_model -> resolve_usable_model), so an
        # unentitled id or an unserved "auto" becomes the first advertised model
        # instead of failing mid-prompt with Invalid model ID. A failed override
        # falls back to the default, except under strict_model.
        if model_to_use and set_model is not None:
            try:
                await set_model(model_to_use)
            except Exception:
                if strict_model:
                    raise
                logger.debug(
                    "bg oneliner: model override to %s failed; using default", model_to_use
                )
            else:
                if strict_model:
                    # set_model can no-op without raising when the id is not
                    # advertised (resolve_usable_model returns ""), so verify the
                    # session SERVES the requested id. Unreadable means refuse.
                    served = str(getattr(session, "served_model", "") or "").strip()
                    if served != model_to_use:
                        raise RuntimeError(
                            "run_bg_oneliner(strict_model=True): session serves "
                            f"{served or 'unknown'!r}, not the required {model_to_use!r}"
                        )
        elif strict_model and model_to_use and set_model is None:
            # No override seam at all: cannot guarantee the model — refuse
            # rather than silently answer from the session default.
            raise RuntimeError(
                "run_bg_oneliner(strict_model=True) requires a session with set_model()"
            )
        # A one-liner's prompt is text ABOUT a session, so an image path in it is
        # quoted history, not an attachment. Inlining it would send image blocks
        # that a text-only background model rejects, so the turn goes out
        # text-only; the scrub only swaps a readable reference for the marker.
        async for event in session.prompt(strip_image_refs(prompt), allow_image=False):
            if event.kind == EVENT_TEXT_CHUNK:
                if max_output_bytes is not None:
                    output_bytes += len(event.text.encode("utf-8"))
                    if output_bytes > max_output_bytes:
                        raise ValueError("background response exceeded its output budget")
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Audit BEFORE rejecting (backend-security-controls), so a
                # reject_tool transport failure cannot skip it. The audit raises:
                # a deny whose row cannot be written is not answered on the wire.
                _sel().log_tool_invocation(
                    session_key=sel_session_key,
                    tool_name=getattr(event, "title", "unknown") or "unknown",
                    outcome="denied",
                    source=sel_source or "bg_oneliner",
                    request_id=str(event.request_id),
                )
                await _steer_host_deny(
                    session,
                    event,
                    "this background one-liner is tool-free by contract; answer "
                    "from the prompt alone",
                    cause=DENY_CAUSE_SURFACE_POLICY,
                )
                await session.reject_tool(event.request_id)
            elif event.kind == EVENT_TOOL_CALL:
                # An AUTO-APPROVED tool arrives with no permission request to
                # reject; audit it so no invocation escapes the SEL log
                # (backend-security-controls).
                _sel().log_tool_invocation(
                    session_key=sel_session_key,
                    tool_name=getattr(event, "title", "unknown") or "unknown",
                    outcome="allowed",
                    source=sel_source or "bg_oneliner",
                )
            elif event.kind == EVENT_COMPLETE:
                _raise_if_unfinished(event, text)
                break
        # kiro-cli's [STEERING steer-…] ack is protocol, not the answer.
        from kiro_crew.messaging.driver import strip_steering_markers

        return strip_steering_markers(text)

    async def _run(model_to_use: str | None) -> str:
        if timeout is not None:
            return await asyncio.wait_for(_drive(model_to_use), timeout)
        return await _drive(model_to_use)

    # Set when this turn (or its one fallback retry) was refused as too long for
    # the model's window; the recycle in the teardown below then forces a fresh
    # provider instead of trusting the reported percentage.
    overflowed = False
    try:
        try:
            return await _run(model)
        except AcpError as exc:
            # The model was rejected mid-prompt (an unserved "auto", or an id the
            # account cannot run). "auto" is never advertised, so it cannot be
            # gated up front: retry ONCE with the first advertised model that is
            # neither the rejected id nor "auto", only when the raise-time
            # classifier tagged a rejected model AND named an advertised set.
            rejected = getattr(exc, "rejected_model", None)
            advertised = getattr(exc, "advertised", None) or []
            fallback = (
                first_advertised_fallback(advertised, rejected)
                if rejected and not strict_model and retry_rejected_model
                else None
            )
            if not fallback:
                raise
            logger.warning(
                "bg oneliner: model %r rejected; retrying once with %r", rejected, fallback
            )
            return await _run(fallback)
    except BaseException as exc:
        overflowed = is_context_overflow_error(exc)
        raise
    finally:
        # Account BEFORE destroy(): the turn's billing lives on the session this
        # tears down. Every one-liner caller records spend here, so none can
        # forget to. The inner finally matters: CancelledError on the persist
        # await is a BaseException, and would otherwise skip destroy() and leak
        # the session's runtime.
        try:
            try:
                # Lazy: the usage module's import chain reaches back into this
                # one (a module-scope import fails on a partially initialized
                # llm_helpers), and it pulls ~600 modules at boot.
                from kiro_crew.dashboard.handlers.usage import persist_token_record_async

                usage = provider_last_turn_usage(session, since=stats_before)
                # The shared predicate: a claude-seam turn can bill cost or cache
                # tokens with zero credits AND zero fresh tokens, which a gate on
                # the kiro dimensions alone would drop.
                if usage_has_billing(usage):
                    _served = str(getattr(session, "served_model", "") or "").strip()
                    _elapsed_ms = int((time.monotonic() - turn_started) * 1000)
                    # The usage store records account spend; the owner's crew log
                    # records spend on THIS session's behalf (nothing when unowned).
                    _record_background_crew_log(
                        _crew_log_owner,
                        crew_log_kind,
                        usage,
                        model=_served,
                        provider=_provider_label(session),
                        elapsed_ms=_elapsed_ms,
                        sessions=sessions,
                        crew_log_session_key=crew_log_session_key,
                    )
                    await persist_token_record_async(
                        sel_session_key,
                        # The SERVED model, never the requested one, which the
                        # fallback may have replaced. An unreadable served model
                        # falls through to model_source rather than a guess.
                        _served,
                        usage,
                        _provider_label(session),
                        surface=f"bg:{sel_source}",
                        elapsed_ms=_elapsed_ms,
                        model_source=session,
                        trigger=TRIGGER_SYSTEM,
                    )
            except Exception:
                logger.debug("bg oneliner accounting failed source=%s", sel_source, exc_info=True)
        finally:
            await session.destroy()
            # A provider-backed handle appended this turn to the ONE shared ``_bg``
            # conversation and ``destroy()`` only released its semaphore; without
            # the recycle that conversation grows until the provider refuses it.
            # A runtime handle's session is ephemeral and carries no marker.
            if getattr(session, "shares_background_conversation", False) is True:
                try:
                    await sessions.recycle_background(context_overflowed=overflowed)
                except Exception:
                    logger.debug("bg oneliner recycle failed source=%s", sel_source, exc_info=True)


def _background_crew_log_owner(sessions: Any, crew_log_session_key: str, crew_log_kind: str) -> str:
    """The crew log unit a background call is charged to, resolved BEFORE the call.

    The resolver answers which unit a slot's work lands in NOW. A slot can be
    reset, switched or compacted while the call is in flight, and the successor
    has a new ACP session id, so a teardown-time lookup would charge this spend
    to a session that did not incur it, in an append-only file.

    Answers ``""`` when the caller named no owner or no kind (shared work, the
    default), when the flag is off, or when the owner cannot be resolved.
    """
    if not crew_log_session_key or not crew_log_kind:
        return ""
    try:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log.resolve import unit_for_session_key

        if not crew_log_emit.enabled():
            return ""
        # The session manager is enough: a background call runs BETWEEN the
        # owner's turns, when the slot holds no live ACP client and the registry
        # is authoritative.
        return unit_for_session_key(sessions, crew_log_session_key)
    except Exception:
        logger.debug(
            "crew log: resolving a background owner failed kind=%s",
            crew_log_kind,
            exc_info=True,
        )
        return ""


def _record_background_crew_log(
    owner_sid: str,
    crew_log_kind: str,
    usage: Any,
    *,
    model: str,
    provider: str,
    elapsed_ms: int,
    sessions: Any = None,
    crew_log_session_key: str = "",
) -> None:
    """File one background model call in the crew log of the session it served.

    ``owner_sid`` is the unit :func:`_background_crew_log_owner` pinned before the
    call. Empty means "do not write".

    The entry lands in the slot's NEWEST unit. A crew log is append-only, and a
    write into a unit the slot has moved past (a reset, switch or compaction during
    the call) grows an older unit, which retires the slot's savepoints beside the
    newest one. So when *crew_log_session_key* now resolves to another unit, the
    entry goes there and counts in that unit's usage; the slot's total is the same.
    An unresolvable key keeps the pinned unit.

    Both background entry points call this after the same ``usage_has_billing``
    gate the usage store uses, so they agree on WHETHER a call is billable. Either
    side can still miss a call the other recorded: the usage store can fail on its
    own path, and this entry's writer can drop it at a ceiling.

    Best-effort: it describes spend, and must never make a background task raise.
    """
    if not owner_sid or not crew_log_kind:
        return
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        target, moved = owner_sid, False
        if sessions is not None and crew_log_session_key:
            from kiro_crew.crew_log.resolve import unit_for_session_key

            current = unit_for_session_key(sessions, crew_log_session_key)
            if current and current != owner_sid:
                target, moved = current, True

        crew_log_emit.on_background_completed(
            target,
            kind=crew_log_kind,
            newer_unit=moved,
            model=model,
            provider=provider,
            credits=float(getattr(usage, "credits", 0.0) or 0.0),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_tokens", 0) or 0),
            duration_ms=int(elapsed_ms),
        )
    except Exception:
        logger.debug(
            "crew log: recording a background call failed kind=%s",
            crew_log_kind,
            exc_info=True,
        )


async def _cleanup_memory_consolidation_session(
    sessions: Any, key: str, memory_store: str, log: Any
) -> None:
    """Retire one generated runtime before removing its private artifacts."""
    try:
        await sessions.remove(key)
    except Exception:
        # A provider that failed to retire may still have a live process or a
        # late PID proof. Preserve both the transcript and binding in that case;
        # deleting authority while the process survives would be unsafe.
        logger.debug("memory consolidation session retirement failed", exc_info=True)
        return
    try:

        # remove() waits for retirement but preserves resumable mappings. This
        # generated UUID has no user continuation, so discard that mapping too.
        await sessions.destroy(key)
        await asyncio.to_thread(log.delete_memory_consolidation_session, key, memory_store)
    except Exception:
        logger.debug("memory consolidation artifact cleanup failed", exc_info=True)


async def _start_fresh_conversation(sessions: Any, key: str, client: Any) -> None:
    """Leave *key* on an empty conversation before its permit is released.

    The cheap path is ``new_conversation()``: a fresh session on the SAME running
    process and the old session's teardown, which removes that one session's
    backend record (KAS ``_kiro/session/delete``; kiro-cli terminate plus the
    guarded unlink of ``sessions/cli/<sid>.json(+.jsonl)``) or, on claude, ends it
    with ``session/close``. If the provider has none or it raises (a backend on
    its own ``AcpClient`` outside ``ACP_BACKENDS_CLIENT_NEW_CONVERSATION`` raises
    ``NotImplementedError``), ``reset`` replaces the process,
    and the key's stateless classification means the next turn starts with no
    history. The same two steps ``WorkerPool.reset`` uses for a reused pool
    worker.

    Logs at INFO which path ran, once per call. Never raises ``Exception``.
    """
    try:
        await sessions.end_children_for(key)
    except Exception:
        logger.debug("ending %s's children before a fresh conversation failed", key, exc_info=True)
    new_conversation = getattr(client, "new_conversation", None)
    if callable(new_conversation):
        try:
            await new_conversation()
        except Exception:
            logger.debug("new_conversation failed for %s", key, exc_info=True)
        else:
            logger.info("Background session %s: fresh conversation on the same runtime", key)
            return
    try:
        await sessions.reset(key, ends_conversation=True)
    except Exception:
        logger.warning(
            "Background session %s: could not start a fresh conversation", key, exc_info=True
        )
        return
    logger.info("Background session %s: reset to a new runtime for a fresh conversation", key)


async def _lite_spec_unconfirmed(
    sessions: Any, key: str, client: Any, *, agent: str, authored_before_spawn: bool
) -> bool:
    """True when a ``kirocrew-lite`` session's spec cannot be confirmed tool-less.

    The background agent reads text Crew does not author (issue bodies, meeting
    transcripts, chat history), so a tool another program wrote into its spec
    would be one that text could reach. Checked like the knowledge pool's worker:
    only for a backend that mounts what the spec names (``Routing.AGENT_SPEC``),
    with the read taken before the spawn and the spec the harness loaded, read
    after it (``effective_spec_declares_zero_tools``). Any other agent, or a
    backend that reads no spec of Crew's, answers False. Never raises.
    """
    from kiro_crew.agent_sdk.backends import Routing, routing_for
    from kiro_crew.session import BACKGROUND_AGENT  # circular import

    try:
        bound = sessions.get_agent(key) if hasattr(sessions, "get_agent") else agent
    except Exception:
        bound = agent
    if bound != BACKGROUND_AGENT:
        return False
    acp = getattr(client, "client", None)
    backend = getattr(acp, "backend", None)
    if not isinstance(backend, str) or routing_for(backend) is not Routing.AGENT_SPEC:
        return False
    confirm = getattr(acp, "effective_spec_declares_zero_tools", None)
    if not callable(confirm):
        return True
    try:
        confirmed = await asyncio.to_thread(confirm, authored_before_spawn=authored_before_spawn)
    except Exception:
        logger.warning("kirocrew-lite spec confirmation failed", exc_info=True)
        return True
    return confirmed is not True


@asynccontextmanager
async def background_turn(
    sessions: Any,
    *,
    task: str,
    agent: "str | None" = None,
    memory_store: str = "",
    crew_log_kind: str = "",
    crew_log_session_key: str = "",
    session_key: str | None = None,
    fresh_conversation: bool = False,
) -> "AsyncIterator[Any]":
    """Take the shared background session for ONE turn, then release and account.

    Acquires the persistent session (taking its per-session semaphore), releases
    that semaphore in a ``finally`` so the next caller cannot deadlock, recycles
    the session, and records the turn's spend in the usage store, so every turn
    taken through this manager is accounted for.

    ``task`` becomes the ``surface`` dimension ``bg:<task>``. Keep it a short
    fixed label: per-session text would make the dimension unbounded.

    ``agent`` is forwarded to ``get_or_create`` ONLY when supplied: it decides
    what the session is created AS, so a default here would change that for
    callers that pass none. The recorded row still names the background agent.

    ``fresh_conversation`` ends the turn's conversation before the permit is
    released, so the next caller on the key starts from an empty context (see
    :func:`_start_fresh_conversation`). It replaces the recycle for that key and
    is ignored with ``memory_store``, whose key already lives for one turn.

    Exceptions are deliberately NOT swallowed. Callers distinguish "the session
    could not be acquired" (nothing was sent, so nothing was billed) from "the turn
    ran and failed" (billed), and collapsing the two corrupts the retry budgets
    built on that distinction. Only the accounting write is best-effort. While host
    memory is critical the turn raises before anything is acquired
    (:func:`_refuse_while_memory_critical`).
    """
    from kiro_crew.session import BACKGROUND_AGENT, BACKGROUND_KEY  # circular import

    key = session_key or BACKGROUND_KEY
    # Pinned before the first suspension point: the owning slot can be reset or
    # recycled while this turn runs, and its successor is a different ledger unit.
    _crew_log_owner = _background_crew_log_owner(sessions, crew_log_session_key, crew_log_kind)
    await _refuse_while_memory_critical(task)
    if memory_store:
        from uuid import uuid4

        from kiro_crew.execution_context import bind_session_execution, execution_for_store
        from kiro_crew.history import ConversationLog
        from kiro_crew.memory_stores import memory_store_version, require_memory_store

        await asyncio.to_thread(require_memory_store, memory_store)
        if memory_store_version(memory_store) != 2:
            raise ValueError("Dedicated member consolidation requires private V2 memory")
        key = f"memory-consolidation:{memory_store}:{uuid4().hex}"
        log = ConversationLog()
        try:
            execution = execution_for_store(memory_store, template_id=agent or "kirocrew")
            await asyncio.to_thread(bind_session_execution, key, execution)
        except BaseException:
            await _cleanup_memory_consolidation_session(sessions, key, memory_store, log)
            raise
    # The pre-spawn read of the background agent's spec, before ``get_or_create``
    # can start the harness on it (see :func:`_lite_spec_unconfirmed`).
    resolved_agent = agent if agent is not None else BACKGROUND_AGENT
    lite_before_spawn = False
    if resolved_agent == BACKGROUND_AGENT:
        from kiro_crew.agent_sdk.agent_specs import authored_spec_toolless_verdict

        verdict = await asyncio.to_thread(authored_spec_toolless_verdict, BACKGROUND_AGENT)
        lite_before_spawn = verdict is True
        if verdict is False:
            # A spec that mounts something is refused before ``get_or_create``:
            # starting the session would run what it names (an MCP server's startup
            # code), which no reset undoes. A missing spec is left to the check
            # after the start.
            logger.warning(
                "Background session %s: %s spec mounts a tool, a grant, an MCP server or "
                "a hook; not starting task=%s",
                key,
                BACKGROUND_AGENT,
                task,
            )
            if memory_store:
                await _cleanup_memory_consolidation_session(sessions, key, memory_store, log)
            raise RuntimeError(
                f"background session {key!r} not acquired: the {BACKGROUND_AGENT} spec "
                "could not be confirmed to mount nothing"
            )
    try:
        if agent is None:
            client, _new, _resumed = await sessions.get_or_create(key)
        else:
            client, _new, _resumed = await sessions.get_or_create(key, agent=agent)
    except BaseException:
        if memory_store:
            await _cleanup_memory_consolidation_session(sessions, key, memory_store, log)
        raise
    # Checked on every turn, not only when ``_new`` says the session is new: a
    # background runtime started by ``_ensure_background`` reports ``_new=False``,
    # and a warm one may have been started on a spec rewritten since. The permit is
    # held from here, so a cancellation during the check gives it back.
    try:
        unconfirmed = await _lite_spec_unconfirmed(
            sessions, key, client, agent=resolved_agent, authored_before_spawn=lite_before_spawn
        )
    except BaseException:
        try:
            sessions.release(key)
        except Exception:
            logger.debug("background session release failed task=%s", task, exc_info=True)
        if memory_store:
            await _cleanup_memory_consolidation_session(sessions, key, memory_store, log)
        raise
    if unconfirmed:
        logger.warning(
            "Background session %s: %s spec mounts a tool, a grant, an MCP server or a "
            "hook; closing the session instead of running task=%s",
            key,
            BACKGROUND_AGENT,
            task,
        )
        try:
            await sessions.reset(key)
        except Exception:
            logger.warning("Background session %s: reset after refusal failed", key, exc_info=True)
        finally:
            try:
                sessions.release(key)
            except Exception:
                logger.debug("background session release failed task=%s", task, exc_info=True)
        if memory_store:
            await _cleanup_memory_consolidation_session(sessions, key, memory_store, log)
        raise RuntimeError(
            f"background session {key!r} not acquired: the {BACKGROUND_AGENT} spec "
            "could not be confirmed to mount nothing"
        )
    # The stats object as it stands BEFORE this turn. The shared session serves
    # many turns, and the runner replaces this object only once a turn actually
    # begins, so identity is what separates a turn that ran from one whose
    # dispatch failed while a previous, already-recorded turn's credits were
    # still installed.
    stats_before = _billing_stats(client)
    # Wall clock for the turn itself, started after the acquire so queue wait is
    # not charged as turn time. The acp provider never fills TurnUsage.duration_ms,
    # so this is the only duration a background row can carry.
    turn_started = time.monotonic()
    try:
        yield client
    finally:
        turn_elapsed_ms = int((time.monotonic() - turn_started) * 1000)
        # Snapshot the billing BEFORE releasing. ``last_prompt_stats`` is shared
        # mutable state on the session: the next waiter's turn carries it over
        # (zeroing credits) or starts accumulating its own, so a read after
        # release attributes that waiter's spend to this task, or loses the row
        # entirely. This read is a synchronous attribute walk, so taking it here
        # costs nothing and keeps release ahead of every await.
        usage = provider_last_turn_usage(client, since=stats_before)
        # Release is synchronous and runs in a ``finally``, so no cancellation can
        # leave this task holding the shared semaphore: CancelledError is a
        # BaseException that no `except Exception` catches. The one await ahead
        # of it, the fresh conversation, runs while the permit is still held, so
        # no waiter can take the key between this turn and the reset.
        try:
            if fresh_conversation and not memory_store:
                await _start_fresh_conversation(sessions, key, client)
        finally:
            try:
                sessions.release(key)
            except Exception:
                logger.debug("background session release failed task=%s", task, exc_info=True)
        # Recycle sits in a finally for the same cancellation reason, and follows
        # accounting because it may replace the provider entirely.
        try:
            try:
                # Same cycle as the oneliner's teardown: the usage module's import
                # chain reaches back into this one (history and several dashboard
                # handlers import ToolApprovalPolicy / run_bg_oneliner from here),
                # so a module-scope import raises ImportError against a partially
                # initialized llm_helpers, and it would pull ~600 modules into
                # every consumer's boot.
                from kiro_crew.dashboard.handlers.usage import persist_token_record_async

                # A turn that never reached the provider bills nothing and has no
                # row to write; the same guard the chat path applies keeps
                # acquire-time failures from landing as zero-credit noise. The
                # shared predicate covers the claude seam's cost and cache
                # dimensions alongside the kiro credits/token signals.
                if usage_has_billing(usage):
                    _record_background_crew_log(
                        _crew_log_owner,
                        crew_log_kind,
                        usage,
                        model=str(getattr(client, "served_model", "") or "").strip(),
                        provider=_provider_label(client),
                        elapsed_ms=turn_elapsed_ms,
                        sessions=sessions,
                        crew_log_session_key=crew_log_session_key,
                    )
                    await persist_token_record_async(
                        key,
                        "",
                        usage,
                        _provider_label(client),
                        surface=f"bg:{task}",
                        agent=agent or BACKGROUND_AGENT,
                        elapsed_ms=turn_elapsed_ms,
                        model_source=client,
                        trigger=TRIGGER_SYSTEM,
                    )
            except Exception:
                logger.debug("background turn accounting failed task=%s", task, exc_info=True)
        finally:
            try:
                if memory_store:
                    await _cleanup_memory_consolidation_session(sessions, key, memory_store, log)
                elif fresh_conversation:
                    # The fresh conversation above already emptied the context.
                    pass
                elif key == BACKGROUND_KEY:
                    await sessions.recycle_background()
                else:
                    await sessions.recycle_background(
                        session_key=key, agent=agent or BACKGROUND_AGENT
                    )
            except Exception:
                logger.debug("background recycle failed task=%s", task, exc_info=True)
