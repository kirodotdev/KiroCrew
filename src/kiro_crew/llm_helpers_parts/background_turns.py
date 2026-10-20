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
) -> str:
    """Stream a single prompt through an ephemeral background session and return
    the accumulated text.

    ``start_priority`` orders the session's start; FOREGROUND only for a caller a
    person is waiting on (rule: ``kiro_crew.start_priority``). ``timeout`` bounds
    the DRIVE only -- see the acquisition below for why it must not be cancelled.

    ``strict_model=True`` makes the requested ``model`` a hard requirement
    rather than a preference: a failed ``set_model`` override raises instead
    of silently degrading to the session default, and the reactive
    rejected-model fallback below is disabled. Callers whose RESULT is only
    meaningful on that exact model (e.g. the poisoned-conversation canary in
    chat_runner, which uses a success as evidence to discard a conversation
    served by that same model) must set it; ordinary best-effort callers
    (title/nav/folder generation) keep the default lenient behavior.

    Consolidates the identical "acquire a ``_bg`` session -> best-effort pin the
    cheap model -> drive the event loop -> ``destroy()`` in ``finally``" skeleton
    that was copied across title, link-label, folder-icon, and session-summary
    generation. The task is tool-free by contract: permission requests are
    rejected and **always** SEL-logged as ``denied`` — every permission decision
    must be audited (``backend-security-controls``). Callers may override
    ``sel_source`` to attribute the denial to their feature; callers that omit it
    are audited under the generic ``"bg_oneliner"`` source rather than silently
    dropping the SEL event.

    ``crew_log_kind`` and ``crew_log_session_key`` name what this call is and which
    session it is charged to, and BOTH are required for it to reach that session's
    crew log. Most callers are not charged to any one session -- a tip, a folder icon,
    a cron label -- so the default is to write nothing rather than attribute shared
    work to whichever session happened to trigger it.

    Errors propagate to the caller (the ``_bg`` session is still ``destroy()``-ed
    in ``finally``): callers that want best-effort "" fallback wrap the call
    themselves, while callers that surface the failure (title/nav) get it
    unchanged. ``sessions`` is duck-typed (a ``SessionManager``-like object
    exposing ``get_bg_session()``) rather than statically imported, so this
    low-level helper stays free of a dashboard/session import cycle.

    ``max_output_bytes`` bounds accumulated UTF-8 text before concatenation;
    ``retry_rejected_model=False`` disables the extra reactive model call for a
    caller that accounts each attempt against a hard call budget. Neither changes
    the configured-model resolution or the default behavior of other callers.
    """
    # Pinned before the acquisition below, not in the teardown that writes it: a
    # slot reset, switch or compaction gives the successor a new ACP session id,
    # and a teardown-time lookup would file this spend under a session that never
    # incurred it. The acquisition is itself a suspension point -- it can take the
    # background runtime lock and start a runtime -- so resolving after it is
    # already late enough to name the successor.
    _crew_log_owner = _background_crew_log_owner(sessions, crew_log_session_key, crew_log_kind)
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
        # Pass the caller's preference (often the governed "auto") to set_model,
        # which resolves it against the session's advertised model list at the
        # wire chokepoint (AcpSessionHandle.set_model -> resolve_usable_model):
        # a hardcoded/unentitled id, or "auto" on a partition that does not serve
        # it, is swapped for the first advertised model instead of
        # reaching the wire and failing mid-prompt with Invalid model ID.
        # Best-effort: a failed override falls back to the default — unless
        # strict_model, where running on any other model would make the
        # result meaningless (see docstring), so the failure propagates.
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
                    # POST-CONDITION, not just no-exception: the substitute-style
                    # set_model seam can silently inherit the session default
                    # when the requested id is absent from the advertised set
                    # (resolve_usable_model returns "" → no-op, no raise). A
                    # strict caller (the poisoned-conversation canary) must
                    # never run on any other model — verify the session now
                    # SERVES the requested id and refuse otherwise. Unreadable
                    # served model ⇒ cannot verify ⇒ refuse.
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
        # A one-liner's prompt is text ABOUT a session (a summary, a title, a
        # label), so any image path in it is quoted history, not an attachment.
        # Inlined, every still-readable file became an image block: a session
        # summary carried one per pasted screenshot, and a text-only background
        # model rejected the whole request on each pass. So the turn goes out
        # text-only, which holds for every shape a caller composes; the scrub
        # only tidies the text, swapping a reference it can read for the marker.
        async for event in session.prompt(strip_image_refs(prompt), allow_image=False):
            if event.kind == EVENT_TEXT_CHUNK:
                if max_output_bytes is not None:
                    output_bytes += len(event.text.encode("utf-8"))
                    if output_bytes > max_output_bytes:
                        raise ValueError("background response exceeded its output budget")
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Audit the denial BEFORE rejecting: every permission decision
                # must be SEL-logged (backend-security-controls), and a
                # reject_tool transport failure must NOT skip the audit.
                # ``sel_source`` carries a non-empty default so callers that
                # don't attribute a feature still produce an audit record.
                # The audit raises: a deny whose row cannot be written is not
                # answered on the wire, so it never proceeds unaudited.
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
                # Tool-free by contract, but an AUTO-APPROVED tool arrives with no
                # permission request to reject — audit it so no invocation escapes
                # the SEL log (backend-security-controls; mirrors the cron/
                # contradiction bg path this helper subsumes).
                _sel().log_tool_invocation(
                    session_key=sel_session_key,
                    tool_name=getattr(event, "title", "unknown") or "unknown",
                    outcome="allowed",
                    source=sel_source or "bg_oneliner",
                )
            elif event.kind == EVENT_COMPLETE:
                _raise_if_unfinished(event, text)
                break
        return text

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
            # Reactive fallback: the model was rejected mid-prompt — e.g. "auto"
            # on a partition that does not serve it, or any id the
            # account cannot run. The advertised list can't be used
            # to gate "auto" statically (it is a sentinel, never advertised), so
            # this is the layer that turns your spec's "else the first available
            # model" into action: retry ONCE with the first advertised model that
            # is neither the rejected id nor "auto". Only fires when the raise-time
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
        # tears down. Every caller of this helper — titles, link labels, folder
        # icons, session summaries, tips, the canary — reaches the provider
        # through here and none of them recorded spend of their own, so the
        # bill moved while the usage store stayed empty. Recording at this one
        # point covers all of them and a new caller cannot forget to.
        # The inner finally is load-bearing: persisting is an await, and
        # CancelledError is a BaseException that no `except Exception` catches,
        # so without it a cancellation landing on that await would skip
        # destroy() and leak the session's runtime.
        try:
            try:
                # Imported here rather than at module scope because the usage
                # module's own import chain reaches back into this one -- history
                # and several dashboard handlers import ToolApprovalPolicy /
                # run_bg_oneliner from here -- so a module-scope import raises
                # ImportError against a partially initialized llm_helpers. It
                # also pulls ~600 modules that every consumer of this low-level
                # module would otherwise pay for at boot.
                from kiro_crew.dashboard.handlers.usage import persist_token_record_async

                usage = provider_last_turn_usage(session, since=stats_before)
                # One shared predicate across every persist gate: a claude-seam
                # turn recovered through the live-stats path can bill cost or
                # cache tokens with zero credits AND zero fresh token counts;
                # a gate testing only the kiro dimensions silently drops it.
                if usage_has_billing(usage):
                    _served = str(getattr(session, "served_model", "") or "").strip()
                    _elapsed_ms = int((time.monotonic() - turn_started) * 1000)
                    # Same numbers, second destination: the usage store answers
                    # "what did the account spend", the owning session's ledger
                    # answers "what was spent on THIS session's behalf". A caller
                    # that names neither a kind nor an owner is work not charged to
                    # any one session (tips, a cron label) and writes nothing.
                    _record_background_crew_log(
                        _crew_log_owner,
                        crew_log_kind,
                        usage,
                        model=_served,
                        provider=_provider_label(session),
                        elapsed_ms=_elapsed_ms,
                    )
                    await persist_token_record_async(
                        sel_session_key,
                        # The model the session SERVED, never the one requested: a
                        # rejected preference is replaced by the reactive fallback
                        # above, so recording the request would bill the spend to a
                        # model that did not run. An unreadable served model falls
                        # through to model_source rather than naming a guess.
                        _served,
                        usage,
                        _provider_label(session),
                        surface=f"bg:{sel_source}",
                        elapsed_ms=_elapsed_ms,
                        model_source=session,
                    )
            except Exception:
                logger.debug("bg oneliner accounting failed source=%s", sel_source, exc_info=True)
        finally:
            await session.destroy()
            # A provider-backed handle appended this turn to the ONE shared ``_bg``
            # conversation; ``destroy()`` only released its semaphore. Without the
            # recycle no criterion bounds that conversation for one-liner traffic,
            # so it grows until the provider refuses it as too long. A runtime
            # handle's session was ephemeral and carries no marker: unchanged.
            if getattr(session, "shares_background_conversation", False) is True:
                try:
                    await sessions.recycle_background(context_overflowed=overflowed)
                except Exception:
                    logger.debug("bg oneliner recycle failed source=%s", sel_source, exc_info=True)


def _background_crew_log_owner(sessions: Any, crew_log_session_key: str, crew_log_kind: str) -> str:
    """The crew log unit a background call is charged to, resolved BEFORE the call.

    Resolution has to happen here rather than in the teardown that writes the
    entry, and the reason is the resolver's own contract: it answers which unit a
    slot's work is landing in NOW. A slot can be reset, switched, or compacted
    while the model call is in flight, and the successor cold-starts a new ACP
    session id -- so a teardown-time lookup would hand this call's spend to a
    session that did not incur it, silently, in an append-only file. Reading it
    before the call pins the unit that was current when the work was ordered.

    Answers ``""`` when the caller named no owner or no kind, which is most of
    them: a background call is shared infrastructure by default, and picking a
    session for a tip or a cron label would put someone else's cost in a user's
    log. Also ``""`` when the flag is off or the owner cannot be resolved.
    """
    if not crew_log_session_key or not crew_log_kind:
        return ""
    try:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log.resolve import unit_for_session_key

        if not crew_log_emit.enabled():
            return ""
        # The session manager the caller already holds is the resolver's only input
        # here, and it is enough: a background call runs BETWEEN the owner's turns,
        # where the owning slot holds no live ACP client and the registry is the
        # authoritative source anyway.
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
) -> None:
    """File one background model call in the crew log of the session it served.

    ``owner_sid`` is the unit :func:`_background_crew_log_owner` pinned before the
    call, never a key resolved here -- see that function for why the timing is the
    whole point. An empty value means "do not write", which covers an unnamed
    caller, a disabled flag and an unresolvable owner alike.

    Both background entry points -- the one-liner and the shared-session context
    manager -- reach this from the same place in their teardown: after the turn's
    usage has been snapshotted and the same ``usage_has_billing`` gate the usage
    store uses has passed. So the two agree on WHETHER a call is billable, which is
    the judgement that would otherwise drift. They do not agree on persistence: the
    usage store writes on its own path and can fail there, and this entry is queued
    for a writer that can drop it at a ceiling, so either side can be missing a call
    the other recorded.

    Best-effort, like the accounting beside it. This is describing spend, not
    controlling it, and it must never be why a background task raises.
    """
    if not owner_sid or not crew_log_kind:
        return
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        crew_log_emit.on_background_completed(
            owner_sid,
            kind=crew_log_kind,
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

    The cheap path is ``new_conversation()``: a fresh ``session/new`` on the SAME
    running process, then the old session's teardown, which removes that one
    session's backend record (KAS ``_kiro/session/delete``; kiro-cli terminate
    plus the guarded unlink of ``sessions/cli/<sid>.json(+.jsonl)``). If the
    provider has none or it raises, ``reset`` replaces the process, and the key's
    stateless classification means the next turn starts with no history. The
    same two steps ``WorkerPool.reset`` uses for a reused pool worker.

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

    Every background caller needs the same three steps around its prompt: acquire
    its persistent session (which takes its per-session semaphore), release that
    semaphore in a ``finally`` or the next caller deadlocks on it, and recycle the
    session afterwards. Callers that hand-rolled those steps had no reason to also
    record what the turn cost, so background spend reached the provider's bill
    without ever reaching the usage store — invisible on the dashboard even though
    the account balance moved. Recording here makes it structural: a turn taken
    through this manager is accounted for, and a new background caller cannot
    forget to.

    ``task`` labels the work in the ``surface`` dimension as ``bg:<task>`` so spend
    is attributable per background job instead of pooling into one anonymous
    bucket. Keep it a short fixed label — never per-session text, which would make
    the dimension unbounded.

    ``agent`` is forwarded to ``get_or_create`` ONLY when a caller supplies it:
    the key decides which session is returned, but the agent decides what the
    session is created AS, so injecting a default here would silently change that
    for callers that deliberately pass none. The recorded row still names the
    background agent so the dimension is never blank.

    ``fresh_conversation`` ends the turn's conversation before the permit is
    released, so the next caller on the key starts from an empty context (see
    :func:`_start_fresh_conversation`). It replaces the recycle for that key and
    is ignored with ``memory_store``, whose key already lives for one turn.

    Exceptions are deliberately NOT swallowed. Callers distinguish "the session
    could not be acquired" (nothing was sent, so nothing was billed) from "the turn
    ran and failed" (billed), and collapsing the two corrupts the retry budgets
    built on that distinction. Only the accounting write is best-effort.
    """
    from kiro_crew.session import BACKGROUND_AGENT, BACKGROUND_KEY  # circular import

    key = session_key or BACKGROUND_KEY
    # Pinned before the first suspension point for the same reason the other
    # background helper does it: the owning slot can be reset or recycled while
    # this turn runs, and its successor is a different ledger unit. Everything the
    # resolver reads is a parameter, so it does not need the session this function
    # is about to acquire.
    _crew_log_owner = _background_crew_log_owner(sessions, crew_log_session_key, crew_log_kind)
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
    try:
        if agent is None:
            client, _new, _resumed = await sessions.get_or_create(key)
        else:
            client, _new, _resumed = await sessions.get_or_create(key, agent=agent)
    except BaseException:
        if memory_store:
            await _cleanup_memory_consolidation_session(sessions, key, memory_store, log)
        raise
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
