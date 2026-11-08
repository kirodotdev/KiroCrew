"""The guards and records every slot-mutating request shares: the per-request commit
token, the replaced-while-queued and busy checks, the attached-sub-agent refusal, the
session reset with its teardown warning, and the explicit agent-selection record.

Their callers are the agent, model, effort, reload, workspace and project routes,
continue and interrupt, the conversation reset, edit-and-resend
(``chat_regenerate.api_chat_slot_edit_resend``), the session-control verbs, and the
send and create bindings.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        KiroCrewConfig,
        LLMProvider,
        ResolvedBindings,
        SelectionChange,
        _cancel_target,
        _ChatSlot,
        _reset_slot_session,
        _test_interleave,
        canonical_key,
        drained_to_thread,
        logger,
        record_agent_selection,
        restore_agent_selection,
        sel,
        subagents_attached_async,
    )


async def _subagents_attached_response(
    state: DashboardState, slot: _ChatSlot, session_key: str, operation: str
) -> web.Response | None:
    """409 while sub-agent children are attached to *session_key*, else None.

    One guard for every endpoint whose action cannot coexist with children —
    dispatching a new turn (continue) interleaves with their writes, and a
    session teardown (reload) kills the shared runtime they run on.

    The probes themselves live in :func:`chat_utils.subagents_attached_async`
    (a coroutine because the queued probe reads the task store), shared
    with the deferred consume in ``chat_runner`` that applies a queued
    conversation discard. That teardown reaches the same runtime without passing
    through any endpoint, so it must apply the same policy — and two copies of
    the probe block is how the two would diverge. This wrapper only shapes the
    refusal.

    The body keeps ``error`` and ``code`` and adds what keeps the session
    attached (:func:`_subagents_attached_detail`), so a user can tell which run
    or row to stop.
    """
    if await subagents_attached_async(state, slot, session_key, operation):
        detail = await _subagents_attached_detail(state, slot, session_key)
        return web.json_response(
            {
                "error": "sub-agents are running",
                "code": "slot_subagents_running",
                "runs": detail["runs"],
                "queued": detail["queued"],
                "queued_partial": detail["queued_partial"],
                "deliveries_inflight": detail["deliveries_inflight"],
            },
            status=409,
        )
    return None


async def _subagents_attached_detail(
    state: DashboardState, slot: _ChatSlot | None, session_key: str
) -> dict[str, Any]:
    """The three things that keep *session_key* attached, as the 409 body names them.

    Each is the source one probe of ``subagents_attached_async`` reads:

    * ``runs``: the unfinished in-memory runs ``running_agents_for`` returns, each
      as ``{"id", "state"}`` where ``state`` is ``stalled`` or ``running``.
      ``None`` when that probe could not answer, the case the guard fails closed on.
    * ``queued``: the accepted spawns with no run yet, read through
      ``handlers.messaging._queued_runs`` (the listing ``GET /api/spawn?queued=1``
      serves): task-store rows, each with its store state (``queued``,
      ``retry_wait``, ``recovering``, ``admitted``), and window entries with no
      row, as ``queued``. ``queued_partial`` is that listing's own flag: True
      when it is capped or its read failed.
    * ``deliveries_inflight``: sub-agent results still being delivered to the slot.

    A probe added to ``subagents_attached_async`` needs its own field here, or the
    409 refuses while naming nothing.

    Ids and states only: no task text, agent name or result. It is read after
    the verdict, so a run that ended in between is simply absent. A probe that
    raises here costs its own field, never the refusal.
    """
    from kiro_crew.dashboard.handlers.messaging import _queued_runs

    subs = getattr(state, "subagents", None)
    runs: list[dict[str, str]] | None = []
    if subs is not None:
        try:
            running = subs.running_agents_for(session_key)
        except Exception:
            running = None
        if running is None:
            runs = None
        else:
            runs = []
            for entry in running:
                if isinstance(entry, Mapping):
                    stalled = bool(entry.get("stalled"))
                    run_id = str(entry.get("id") or "")
                else:
                    stalled, run_id = False, str(entry)
                runs.append({"id": run_id, "state": "stalled" if stalled else "running"})
    listing = await _queued_runs(state, session_key)
    return {
        "runs": runs,
        "queued": [
            {"id": str(q.id), "state": str(getattr(q, "state", "") or "queued")}
            for q in listing.runs
        ],
        "queued_partial": bool(listing.partial),
        "deliveries_inflight": int(getattr(slot, "_subagent_deliveries_inflight", 0) or 0),
    }


async def _reset_slot_session_or_warn(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    *,
    switch_kind: str,
) -> bool | None:
    """:func:`_reset_slot_session` for the commit-before-reset switch handlers.

    Returns the reset verdict, or ``None`` when the teardown RAISED after the
    session pop. The model and workspace handlers commit the new setting
    BEFORE this await, and ``SessionManager.reset`` pops the session before
    its shutdown can fail, so a post-pop raise is a success with a degraded
    teardown — the committed value is what every replacement session runs.
    That premise is VERIFIED, not assumed: a raise with the SAME provider
    instance still registered (a pre-pop failure, e.g. in the pending-wait
    unblock) is re-raised, because the old session then survives on the old
    value and a 200 would be a false success. Identity, not presence: a
    successor session registered by a concurrent send after the pop must not
    be misclassified as the unpopped old one. Propagating a post-pop raise
    instead would answer 500 without ever reaching
    ``state.push_slots_update()``, leaving every
    connected client rendering the OLD value over a switch that actually
    happened (the acting tab's ``performSlotSwitch`` also keeps its old store
    value on a non-2xx). ``None`` tells the caller to record the degraded
    teardown and answer 200 with :data:`_TEARDOWN_INCOMPLETE_WARNING` after
    the usual slots push; a caller with a rebind guard
    (``effective_session_key`` — the model and workspace handlers) must still
    FALL THROUGH and run it, so a slot rebound during the raising await keeps
    answering rollback + 409. Shared
    by all four commit-before-reset switch handlers (agent, reasoning effort,
    model, workspace) so none of them repeats the try block: each calls twice
    (first attempt + idle-decline retry). Only the raise path is
    handled here: a normal ``bool`` verdict passes through untouched, so the
    decline (``False``) ladders keep their semantics.

    ``skip_if_busy`` is fixed at True: every caller is a switch handler with
    a decline ladder (agent, reasoning effort, model, workspace), and
    :func:`_reset_slot_session` must decline a busy session atomically with
    the pop rather than tear it down mid-turn — the ladder then disambiguates
    the decline. A caller that wants every raise to propagate keeps using
    :func:`_reset_slot_session` directly.
    """
    # Captured BEFORE the await: the post-raise probe must compare INSTANCE
    # IDENTITY, not mere presence. A concurrent send can register a SUCCESSOR
    # session for the same key after the pop and before the old session's
    # shutdown raises — a bare "is a provider registered?" probe would
    # misclassify that successor as the unpopped old session and answer 500
    # for a committed switch. The successor
    # cold-started from the slot's CURRENT (committed) bindings, so the
    # committed-success answer is truthful for it.
    prior_provider = state.sessions.get_provider(session_key)
    if _test_interleave is not None:
        # Inside the caller's locks, after it committed its new setting, before
        # the old session goes -- the span a concurrent teardown must be able to
        # land in for the ordering to be observable at all. Placed on this shared
        # helper rather than in each handler because all four commit-before-reset
        # switches reach the teardown through here, and a point per handler is how
        # one of them ends up without one.
        await _test_interleave("switch:post_commit")
    try:
        return await _reset_slot_session(state, slot, session_key, skip_if_busy=True)
    except Exception:
        if (
            prior_provider is not None
            and state.sessions.get_provider(session_key) is prior_provider
        ):
            # The SAME instance is still registered: the raise came BEFORE the
            # session pop (e.g. the pending-wait unblock that
            # _reset_slot_session runs first), so the old session is still
            # alive on the old value and a 200 here would be exactly the false
            # success the switch handlers' decline ladders treat as worse than
            # any retryable error. Propagate: the committed-switch answer is
            # only truthful once the pop has happened.
            raise
        logger.exception(
            "Slot %s %s switch: old session teardown incomplete", slot.key, switch_kind
        )
        return None


async def _record_explicit_agent_selection(
    session_key: str,
    agent_name: str | None,
    bindings: ResolvedBindings,
    *,
    config: KiroCrewConfig,
    memory_mode: str = "persistent",
    app: str = "",
) -> SelectionChange | None:
    """Capture the admitted choice and drain publication before cancellation."""
    from kiro_crew.execution_context import (
        ExecutionContext,
        MemoryStoreRef,
        resolve_member_execution,
    )

    selected = agent_name or bindings.resolved_alias
    if not selected and bindings.selection_kind == "template":
        # A plain session selected its template (the default one): record it,
        # so the conversation stays on it whatever is discovered later.
        selected = bindings.kiro_agent
    if bindings.selection_kind == "member":
        bindings.execution_context = resolve_member_execution(
            config, selected, memory_mode=memory_mode, app=app, validate_memory_files=False
        )
    else:
        bindings.execution_context = ExecutionContext(
            None,
            MemoryStoreRef(bindings.memory_store_name or "default"),
            "template",
            bindings.kiro_agent,
            memory_mode,
            app=app,
            selection_name=selected,
        )
    writer = asyncio.create_task(
        asyncio.to_thread(
            record_agent_selection,
            session_key,
            agent_name,
            bindings,
            replace=True,
            memory_mode=memory_mode,
        )
    )
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            change = await asyncio.shield(writer)
            break
        except asyncio.CancelledError as exc:
            if writer.cancelled():
                raise
            cancelled = exc
        except Exception:
            if cancelled is not None:
                raise cancelled from None
            raise
    if cancelled is not None:
        await drained_to_thread(restore_agent_selection, session_key, change)
        raise cancelled
    # The backend and model selection read the worker set in memory: a worker
    # switched to an ordinary template leaves it at the commit, not at its next turn.
    from kiro_crew.mate_worker import note_worker_session

    note_worker_session(session_key, bindings.execution_context)
    return change


class _CommitToken(str):
    """A ``str`` whose per-request IDENTITY marks commit ownership.

    ``api_chat_slot_agent`` commits ``slot.agent`` (and the derived
    ``slot.workspace`` / ``slot.project`` / ``slot.memory_store``) before its
    awaits and may have to roll those commits back (session rebound, busy
    decline). Every one of
    those fields has unlocked writers (openai_compat, members, the in-turn
    /agent and set_project directives), so the rollback must not fire when
    one of them wrote during the awaits — including a write of the SAME text,
    which a value compare-and-set cannot distinguish from this handler's own
    commit (the in-turn set_project directive can legitimately write the very
    project this handler derived). A subclass instance compares, hashes,
    serializes and persists exactly like the plain string, but is a distinct
    object per commit: ``slot.<field> is <token>`` is therefore a sound
    "still my write" test with no cooperation needed from the other writers.
    """

    __slots__ = ()


def _slot_replaced_while_queued(
    state: DashboardState,
    slot: _ChatSlot,
    name: str,
    request: web.Request | Mapping[str, Any],
    operation: str,
) -> bool:
    """Whether ``name`` registers a different object than *slot* -- checked after a lock await.

    Every switch handler (and reload) reads ``state._slots.get(name)`` before
    its first await, then queues on ``slot._lock``, the session-keyed switch
    lock and, on the model paths, ``slot._model_pick_lock``. Slot removal and
    same-name re-registration take NONE of those locks (a client reconnecting,
    or a different app claiming the name), so by the time a queued request
    resumes, ``name`` can belong to a different slot object. Everything the
    handler does next -- the app-isolation check, the busy probe, the reset --
    reads the STALE object, and an unlinked replacement resolves to the very
    same ``dashboard:<name>`` session key, so the stale request's authorization
    lands its teardown on the replacement's session. Same cross-slot-identity
    gap ``chat_tags.py`` and ``_reauthorize_after_await`` close with this exact
    ``is not slot`` test; reload and its five switch siblings share it here.

    Call it immediately after EVERY lock-acquisition await and before any
    read of ``slot`` that feeds an authorization or a teardown -- not once at
    the end, because each await is its own window. A mismatch is audited as an
    ``api_access`` denial (an app caller's under ``app_isolation``, a dashboard
    caller's like the tags handler's) and the caller answers the same 404 a
    missing slot gets, so a denial cannot be told from a name that never
    existed (``slot_ownership.slot_not_found``).
    """
    if state._slots.get(name) is slot:
        return False
    request_app = request.get("app", "")
    sel().log_api_access(
        caller=request_app or "dashboard",
        operation=operation,
        outcome="denied",
        source="app_isolation" if request_app else "dashboard",
        resources=f"slot={name}",
        error="slot was replaced while the request queued on the switch locks",
    )
    return True


def _switch_target_busy(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    provider: object,
    *,
    own_turn: bool = False,
) -> bool:
    """Whether a turn is in flight on *session_key* -- the switch handlers' pre-commit refusal.

    Every switch handler that resets the live session (agent, model, bulk
    model, reasoning effort, workspace) refuses BEFORE it commits so nothing
    needs rolling back. The refusal reads three signals, and each one sees a
    window the others miss:

    * ``slot.running`` -- set at dispatch, BEFORE the multi-second
      ``provider.start()`` registers a session, so a cold-starting first
      turn dispatched through THIS slot is visible here and nowhere else.
    * ``provider.has_active_turn()`` -- the registered provider's own view,
      which also sees a channel-linked turn that runs under the shared key
      without ever setting this slot's ``task``. *provider* is whatever
      ``state.sessions.get_provider(session_key)`` answered the caller just
      before this call (the caller reads it once and may need it afterwards).
      ``isinstance``, not a None check: the base class documents that
      caller-side guards defend against test doubles that are not
      ``LLMProvider`` instances, and the base default is False so no real
      provider is missed.
    * every OTHER running slot's turn key -- ``_cancel_target`` of each
      slot whose ``task`` is live: the identity its in-flight turn published
      (``_active_turn_session_key``), falling back to its routing when the
      turn has not published yet; compared after ``canonical_key`` so a
      legacy bare Slack key and its ``slack:`` sibling name one session. Two alias slots can drive ONE session (a
      channel-linked slot and its dashboard twin), and a switch issued
      through alias A while alias B is cold-starting sees neither of the
      first two signals: A's ``running`` is False and B's provider is not
      registered yet. Without this scan the switch commits, resets nothing,
      and reports success while the session comes up on B's captured (old)
      bindings -- the header advertises one agent/model/workspace and the
      live process runs another. The TURN key, not the routing: a rebind
      landing on B mid-turn (a cron injection takes no ``running`` gate)
      moves B's routing off the shared session while its turn still runs
      there, and a scan of routings would drop B from the set exactly when
      its turn is what the reset would tear down.

    Call it INSIDE the switch locks, with the *session_key* resolved there
    (the value the probe and the reset act on). Callers keep the atomic
    ``skip_if_busy`` decline in ``SessionManager.reset`` as the backstop for
    a turn that starts after this read: message dispatch takes none of the
    switch locks, so this is a fast path, not the authority. The two teardown
    routes read it the same way: reload inside the locks it shares with the
    switches, reset-conversation (which takes no switch lock) on the key it
    then discards with ``skip_if_busy``.

    *own_turn* is the in-turn ``/agent <name>`` command (``switch_slot_agent``
    called from the slot's own turn, before it acquires a session): that turn
    is the switch itself and never prompts the provider, so its own
    ``running`` flag is not a turn in flight. The provider probe and every
    OTHER slot's turn still refuse.
    """
    if slot.running and not own_turn:
        return True
    if isinstance(provider, LLMProvider) and provider.has_active_turn():
        return True
    # list(): the scan is read-only and message dispatch may register a slot
    # while it runs (the same snapshot every other slot-table scan takes).
    # Compared in CANONICAL spelling: a slot restored from an old transcript
    # can carry the bare legacy Slack ``thread_ts`` while its sibling carries
    # ``slack:<thread_ts>``; SessionManager folds both onto one session, so
    # the comparison must too (``slot_switch_session_lock`` keys the same way).
    target = canonical_key(session_key)
    return any(
        other.running
        and not (own_turn and other is slot)
        and canonical_key(_cancel_target(other)) == target
        for other in list(state._slots.values())
    )
