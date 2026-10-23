"""Deliver a sub-agent completion into its parent.

The dashboard tab that shows the parent (a turn of its own, a queued card, or the
bell once the tab has closed), a channel or session parent with no tab (its ACP
session takes the turn and only the reply reaches the conversation), a cron
parent, and the bell. A queued or dispatched hand-off owes its delivery marks to
the turn that consumes it.

Orchestrator fields read: ``_cfg``, ``_cron_injecting``, ``_cron_runs_holding``,
``_inline_parked``, ``_inline_tasks``, ``_owner_id``, ``conv_log``, ``cron_svc``,
``ctx_builder``, ``dashboard_state``, ``sessions``, ``slack``, ``subagent_mgr``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from kiro_crew.slack.gateway import (
        INJECTION_TIMEOUT,
        SLACK_NAMESPACE,
        SUBAGENT_COMPLETION_KIND,
        SUBAGENT_COMPLETION_META_KEY,
        Any,
        GatewayOrchestrator,
        SubagentDelivery,
        SubagentInfo,
        _get_agent_for_session,
        _start_next_queued_turn,
        _subagent_queued_count,
        build_options_blocks,
        build_timing_footer,
        channel_namespace_of,
        dashboard_slot_key,
        drain_idle_parent_queue_for_stall,
        extract_options,
        idle_slot_admits_drain,
        is_legacy_slack_key,
        is_thread_incognito,
        is_thread_temporary,
        ledger_of,
        load_window_off_loop,
        logger,
        mint_options_token,
        redact_credentials,
        redact_exfiltration_urls,
        render_for_slack,
        run_in_embed_pool,
        save_conversation_turn_off_loop,
        session_store_for_turn,
        settles_at_release,
        subagent_event_slot,
    )


def _delivered_inline(mgr: Any, carried: tuple[str, ...]) -> bool:
    """True when every member *carried* names already reached its parent inline.

    The one question each completion route asks before it injects. A member
    counts only when its live record carries ``_collected_inline``, which
    ``POST /api/spawn/mark-collected`` sets once the parent's own blocking
    ``spawn_sub_agents`` reply holding it was written to the parent's pipe. An
    empty announce, an unknown or evicted record, or any unmarked member answers
    False, so the announce is delivered: a duplicate at worst, never a loss.
    """
    if mgr is None or not carried:
        return False
    for aid in carried:
        record = mgr.get(aid)
        if record is None or getattr(record, "_collected_inline", False) is not True:
            return False
    return True


#: The three answers :func:`_inline_verdict` gives a route whose parent settles at
#: release (``settles_at_release``).
INLINE_CONFIRMED = "confirmed"
INLINE_PENDING = "pending"
INLINE_DELIVER = "deliver"


def _inline_verdict(mgr: Any, sessions: Any, carried: tuple[str, ...]) -> tuple[str, Any]:
    """What a route does with an announce whose parent settles at release:
    ``(verdict, holding)``.

    * ``confirmed`` -- every member it carries was returned inline in a turn that
      has landed (``kiro_crew.parent_turns``): acknowledge it, no second turn.
    * ``pending`` -- every member was returned inline in the turn still holding
      the parent's permit, under one holding: park it until that holding's
      release settles it (``_park_inline``), never waiting inside ``_on_done``.
    * ``deliver`` -- anything else, a revoked mark included: deliver it.

    A pending answer needs the ledger to still hold the marks and a settle hook
    to be wired, so nothing is parked that no release would settle.
    """
    if not _delivered_inline(mgr, carried):
        return INLINE_DELIVER, None
    states = {getattr(mgr.get(aid), "_inline_state", "") for aid in carried}
    if states == {INLINE_CONFIRMED}:
        return INLINE_CONFIRMED, None
    tokens = {getattr(mgr.get(aid), "_inline_token", None) for aid in carried}
    ledger = ledger_of(sessions)
    if (
        states == {INLINE_PENDING}
        and len(tokens) == 1
        and ledger is not None
        and ledger.on_released is not None
    ):
        (token,) = tokens
        if token is not None and ledger.pending(token):
            return INLINE_PENDING, token
    return INLINE_DELIVER, None


@dataclass
class _ParkedCompletion:
    """A completion whose members' turn had not released when it arrived.

    ``own`` is the member's own delivery mark and ``held`` the wave siblings its
    digest carries: both are owed until the completion is settled. ``sub_meta``
    is the card a tab route queues it with.
    """

    info: SubagentInfo
    route: str  # "cron", "channel" or "tab"
    announce: str
    body: str
    title: str
    carried: tuple[str, ...]
    parent_key: str
    own: list[SubagentDelivery]
    held: list[SubagentDelivery]
    sub_meta: dict[str, Any] = field(default_factory=dict)


def _park_inline(
    self: GatewayOrchestrator,
    token: Any,
    info: SubagentInfo,
    *,
    route: str,
    announce: str,
    body: str,
    title: str,
    carried: tuple[str, ...],
    parent_key: str,
    sub_meta: dict[str, Any] | None = None,
) -> None:
    """Hold a completion until the holding ``token`` releases, owing its tombstone.

    As a queued tab completion does (``_defer_queued_delivery``), the delivery
    marks it owes are taken off the record and ``_delivery_queued`` stops the run
    loop from writing its own ``delivered`` tombstone. So ``_on_done`` returns at
    once, the result TTL has not started, and a restart before the settle finds
    the folder untombstoned and re-announces it. Synchronous: nothing can settle
    the holding between the verdict that chose to park and this append.
    """
    # As _defer_queued_delivery owes them: a flush-only record is synthetic (no
    # run, no folder of its own), so only a real member owes its own mark.
    flush_only = getattr(info, "_digest_flush_only", False) is True
    own: list[SubagentDelivery] = []
    if not flush_only and info.outcome == "completed":
        own.append(SubagentDelivery(info.id, info.elapsed, info.credits))
    elif not flush_only and getattr(info, "_report_owed", False) is True:
        own.append(SubagentDelivery(info.id, info.elapsed, info.credits, report_owed=True))
    held: list[SubagentDelivery] = []
    snapshot = getattr(info, "_digest_settle_deliveries", None)
    if isinstance(snapshot, list):
        held.extend(snapshot)
        info._digest_settle_deliveries = []
    info._delivery_queued = True
    completion = _ParkedCompletion(
        info, route, announce, body, title, carried, parent_key, own, held, dict(sub_meta or {})
    )
    self._inline_parked.setdefault(token, []).append(completion)
    logger.info(
        "Subagent %s: parked until the %s turn that received it inline releases",
        info.id,
        parent_key,
    )


def _settle_inline(
    self: GatewayOrchestrator, token: Any, landed: bool, child_ids: list[str]
) -> None:
    """The ledger's release hook: confirm or revoke ``token``'s marks, then settle
    the completions parked on it.

    Runs synchronously inside ``SessionManager.release``, so every record reads
    its new state before anything else runs, and a re-routed completion of a cron
    parent is counted in ``_cron_injecting`` before the cron ``finally`` decides
    whether to reset the session it is delivered into. A landed turn's marks also
    settle their children on a tab that shows the parent, as the mark itself does
    for a dashboard-born parent (``_settle_collected_on_slot``): a completion of
    theirs already queued there is removed, never played as a second turn.
    """
    mgr = self.subagent_mgr
    confirmed: dict[str, set[str]] = {}
    for aid in child_ids:
        record = mgr.get(aid) if mgr is not None else None
        if record is None or getattr(record, "_inline_token", None) != token:
            continue
        if landed:
            record._inline_state = INLINE_CONFIRMED
            confirmed.setdefault(str(record.parent_session_key), set()).add(aid)
        else:
            record._collected_inline = False
            record._inline_state = ""
            record._inline_token = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Nothing can be scheduled off the loop: what was parked stays parked and
        # owed, its folders untombstoned, for the next start to re-announce.
        logger.warning("Settled %s off the event loop; its parked results wait", token[0])
        return
    for completion in self._inline_parked.pop(token, []):
        # A cron parent's cron or tab route delivers into the cron session (the
        # tab's turn runs on it), so the cron turn's own cleanup must not reset
        # it first. A channel route never serves a cron parent.
        counted = not landed and completion.parent_key.startswith("cron:")
        if counted:
            key = completion.parent_key
            self._cron_injecting[key] = self._cron_injecting.get(key, 0) + 1
        task = loop.create_task(self._finish_parked(completion, counted=counted))
        self._inline_tasks.add(task)
        task.add_done_callback(self._inline_tasks.discard)
    state = self.dashboard_state
    if state is None or not confirmed:
        return
    from kiro_crew.dashboard.handlers.messaging import _settle_collected_on_slot

    for parent_key, ids in confirmed.items():
        slot_name = dashboard_slot_key(parent_key)
        slot = state.get_slot(slot_name) if slot_name else None
        if slot is None:
            continue
        try:
            owed = _settle_collected_on_slot(slot, ids, state.subagents)
        except Exception:
            # The queued completion plays as a turn: a duplicate, never a loss.
            logger.debug("Could not settle returned results on %s", slot_name, exc_info=True)
            continue
        if owed and mgr is not None:
            settle = loop.create_task(_settle_owed(mgr, owed))
            self._inline_tasks.add(settle)
            settle.add_done_callback(self._inline_tasks.discard)
        state.push_slots_update()


async def _finish_parked(
    self: GatewayOrchestrator, completion: _ParkedCompletion, *, counted: bool
) -> None:
    """Run a settled parked completion through its route, then pay what it owes.

    The route reads the settled state: a confirmed mark is acknowledged with no
    second turn, a revoked one is delivered. A tab completion is routed again from
    the tab the parent shows now (``_route_parked_in_tab``). The owed marks go back
    on the record first, so a route that hands the completion to a tab's turn or
    queue owes them to that consumption, as it does a fresh completion's
    (``_defer_queued_delivery``). Whatever no route took on is written once the
    route delivered (``settle_queued_delivery``); a route that gave the report up
    leaves the folder untombstoned, for the next start to re-announce. A completion
    whose parent was retired, by its own id or a member it carries or holds
    (``suppress_retired_delivery``), takes no route at all.
    """
    info = completion.info
    info._digest_settle_deliveries = list(completion.held)
    info._delivery_queued = False
    mgr = self.subagent_mgr
    members = [*completion.carried, *(delivery.agent_id for delivery in completion.held)]
    if mgr is not None and mgr.suppress_retired_delivery(info, members) is True:
        # Its parent was retired: no route runs, and the held siblings stay
        # undelivered. Its own marks are written, as a fresh completion's tombstone
        # is after the terminal report skips it, and a counted cron re-route gives
        # its count back and runs the deferred reset as ``_route_parked_in_tab``
        # does when its route ends.
        parent_key = completion.parent_key
        try:
            if completion.own:
                await _settle_owed(mgr, completion.own, info.id)
        finally:
            if counted:
                self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 1) - 1
                if self._cron_injecting[parent_key] <= 0:
                    self._cron_injecting.pop(parent_key, None)
                await self._reset_cron_parent_when_idle(info, parent_key)
        return
    if completion.route == "tab":
        await self._route_parked_in_tab(completion, counted=counted)
    elif completion.route == "cron":
        await self._finish_subagent_cron_or_notify(
            info,
            announce=completion.announce,
            body=completion.body,
            title=completion.title,
            carried=completion.carried,
            parent_key=completion.parent_key,
            injection_counted=counted,
        )
    else:
        await self._inject_subagent_into_channel_parent(
            info,
            announce=completion.announce,
            body=completion.body,
            title=completion.title,
            carried=completion.carried,
            parent_key=completion.parent_key,
        )
    if mgr is None or info._delivery_queued or info._report_undelivered:
        return
    owed = [*completion.own, *info._digest_settle_deliveries]
    info._digest_settle_deliveries = []
    if owed:
        await _settle_owed(mgr, owed, info.id)


async def _settle_owed(mgr: Any, owed: list[SubagentDelivery], agent_id: str = "") -> None:
    """Write the delivery marks a settled completion owes; a failure is logged."""
    try:
        await mgr.settle_queued_delivery(owed)
    except Exception:
        logger.debug("Subagent %s: could not settle its delivery marks", agent_id, exc_info=True)


async def _route_parked_in_tab(
    self: GatewayOrchestrator, completion: _ParkedCompletion, *, counted: bool
) -> None:
    """Route a settled tab completion as ``_subagent_done`` routes a fresh one.

    The parent's tab is looked up again (``_parent_tab``), and the completion takes
    the route that gives: the tab, or the parent's own route once the tab has
    closed or its window cannot be read. A revoked completion of a cron parent was
    counted in ``_cron_injecting`` inside the release, so the cron turn's
    ``finally`` deferred its reset. When the tab takes it, the count is held until
    the turn that delivers it ends, then the deferred reset runs
    (``_reset_cron_parent_when_idle``); the cron route takes the count over.
    """
    info, parent_key = completion.info, completion.parent_key
    taken = ""
    try:
        try:
            slot_name, slot, event_slot = await self._parent_tab(parent_key)
        except Exception:
            # A restored tab whose window cannot be read is treated as closed:
            # the parent's own route delivers the completion now, rather than
            # leaving it to the next start's re-announce.
            logger.warning(
                "Parked completion %s: the tab showing %s could not be read; "
                "delivering it through the parent's own route",
                info.id,
                parent_key,
                exc_info=True,
            )
            slot_name, slot, event_slot = "", None, subagent_event_slot(parent_key)
        taken = await self._route_completion(
            info,
            announce=completion.announce,
            body=completion.body,
            title=completion.title,
            sub_meta=completion.sub_meta,
            carried=completion.carried,
            flush_only=getattr(info, "_digest_flush_only", False) is True,
            parent_key=parent_key,
            slot_name=slot_name,
            slot=slot,
            event_slot=event_slot,
            injection_counted=counted,
        )
        delivering = getattr(slot, "task", None) if taken == "tab" else None
        if counted and isinstance(delivering, asyncio.Task) and not delivering.done():
            await asyncio.wait({delivering}, timeout=INJECTION_TIMEOUT)
    finally:
        if counted and taken != "cron":
            self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 1) - 1
            if self._cron_injecting[parent_key] <= 0:
                self._cron_injecting.pop(parent_key, None)
            await self._reset_cron_parent_when_idle(info, parent_key)


async def _parent_tab(self: GatewayOrchestrator, parent_key: str) -> tuple[str, Any, str]:
    """The tab showing *parent_key*: ``(slot name, slot, the slot its events carry)``.

    The slot name is empty when no tab shows the parent. A tab restored as a
    sidebar row loads its window first, off the loop, so a completion delivered
    into it lands in its transcript.
    """
    slot_name = dashboard_slot_key(parent_key)
    slot = None
    if self.dashboard_state and slot_name:
        from kiro_crew.dashboard.handlers.messaging import _slot_for_parent

        slot = _slot_for_parent(self.dashboard_state, parent_key)
        if slot is None:
            slot = self.dashboard_state.get_slot(slot_name)
        if getattr(slot, "window_pending", False) is True:
            await load_window_off_loop(slot)
            slot = _slot_for_parent(self.dashboard_state, parent_key) or (
                self.dashboard_state.get_slot(slot_name)
            )
    key = getattr(slot, "key", "")
    return slot_name, slot, key if isinstance(key, str) and key else subagent_event_slot(parent_key)


async def _route_completion(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    *,
    announce: str,
    body: str,
    title: str,
    sub_meta: dict[str, Any],
    carried: tuple[str, ...],
    flush_only: bool,
    parent_key: str,
    slot_name: str,
    slot: Any,
    event_slot: str,
    injection_counted: bool = False,
) -> str:
    """Route a completion back to the session it came from; return the route taken.

    Tab open: that tab (a channel-born tab mirrors on to its channel), ``"tab"``.
    Channel or session parent with no tab: its own session and channel,
    ``"channel"``. Cron parent or none: the cron session and the bell, ``"cron"``.
    """
    if slot_name and self.dashboard_state:
        await self._route_subagent_to_tab(
            info,
            announce=announce,
            body=body,
            title=title,
            sub_meta=sub_meta,
            carried=carried,
            _flush_only=flush_only,
            _injection_slot=slot,
            _injection_slot_name=event_slot,
            parent_key=parent_key,
        )
        return "tab"
    if parent_key and not parent_key.startswith(("cron:", "subagent:")):
        await self._inject_subagent_into_channel_parent(
            info,
            announce=announce,
            body=body,
            title=title,
            carried=carried,
            parent_key=parent_key,
        )
        return "channel"
    await self._finish_subagent_cron_or_notify(
        info,
        announce=announce,
        body=body,
        title=title,
        carried=carried,
        parent_key=parent_key,
        injection_counted=injection_counted,
    )
    return "cron"


def _injection_slot_busy(slot: Any) -> bool:
    """True when *slot* already owns a turn a new injection must wait behind.

    ``slot.running`` alone is not enough. A just-dispatched injection parks in
    ``bounded_chat_turn``'s off-loop timeout resolution before ``_run_chat``
    starts, and only the live ``slot.task`` — assigned synchronously at
    dispatch — records that claim. A slot whose ``running`` is not derived
    from ``task`` (test doubles, duck-typed slots) reads such a window as
    idle, so a later digest chunk takes the idle branch: it appends in
    whichever order the dispatch hops resolve (not FIFO under CPU load) and
    assigns ``slot.task`` over the earlier chunk's still-pending task instead
    of awaiting it. Consulting the claim directly keeps chunk delivery FIFO
    regardless of how ``running`` is implemented or when the hop resolves.
    """
    task = slot.task
    return bool(slot.running) or (task is not None and not task.done())


def _skip_inline_collected(slot: Any, child_id: str) -> None:
    """Forget *child_id*'s inline collection and withdraw only its own arm.

    The blocking spawn tool already returned this result, so this child's arm
    has nothing left to summarize. The synthesis is dropped only when no other
    child armed it: a background run that finished alongside the wave still
    owes the parent its turn. An inline-collected child can only have armed on
    the busy path, at entry, before the blocking tool's collection landed.
    """
    slot._subagents_inline_collected.discard(child_id)
    if child_id in slot._synthesis_armers:
        slot._synthesis_armers.discard(child_id)
        if not slot._synthesis_armers:
            slot._pending_synthesis = False


# Max attempts, the first one included, to inject a subagent result into a
# parent session.
_MAX_INJECT_ATTEMPTS = 2


def _defer_queued_delivery(
    slot: Any, announce: str, info: SubagentInfo, *, flush_only: bool
) -> None:
    """Owe a queued completion's delivery tombstones to the queue drain.

    The retention window for ``result.txt`` (``agent.subagent_result_ttl_secs``)
    exists so the parent can read the full transcript AFTER the completion
    event reaches it. Writing the ``delivered`` tombstone when the announce is
    merely QUEUED starts that clock while the event is still waiting for a
    turn, so a long-running turn ahead of it lets the reaper prune every file
    the queued announce points at.

    So the ids are handed to the slot keyed on the announce ITSELF,
    ``_delivery_queued`` tells the run loop to skip its own ``mark_delivered``,
    and the drain (``chat_runner._start_next_queued_turn``) settles them once a
    turn has consumed that announce -- including a retry, because a failure
    before the model consumed the prompt re-queues the same text under a newly
    minted queue id, which a debt keyed on the original id could never match. A
    wave digest carries its held members' snapshots too:
    ``_digest_settle_deliveries`` is
    transferred (not copied), so the run loop's ``_settle_digest_holds`` becomes
    a no-op rather than a second writer.

    Best-effort in one direction only: if the slot cannot take the ids (a
    stubbed slot in tests), nothing is transferred and the previous
    immediate-tombstone behaviour stands — better a short window than a
    folder no one ever tombstones.
    """
    # A flush-only record is synthetic (no run, no folder of its own). Only a
    # COMPLETED member owes a delivered mark: ``info.outcome`` is the codebase's
    # canonical three-way classification precisely because the ``error``-
    # nullability idiom reports a user-stopped agent as completed, and a
    # stopped or failed run already carries its own tombstone whose 7-day
    # post-mortem window a "delivered" write would shorten to the result TTL.
    # A memory-wait expiry has no folder either, but the store owes its report
    # until the announce is consumed, so its debt is carried the same way.
    owed: list[SubagentDelivery] = []
    if not flush_only and info.outcome == "completed":
        owed.append(
            SubagentDelivery(
                info.id,
                info.elapsed,
                info.credits,
                leaked_process=getattr(info, "_leaked_process", False) is True,
            )
        )
    elif not flush_only and getattr(info, "_report_owed", False) is True:
        owed.append(SubagentDelivery(info.id, info.elapsed, info.credits, report_owed=True))
    held = getattr(info, "_digest_settle_deliveries", None)
    if isinstance(held, list):
        owed.extend(held)
    try:
        slot.note_pending_subagent_delivery(announce, owed)
    except Exception:
        logger.debug("Subagent %s: could not defer queued delivery marks", info.id, exc_info=True)
        return
    info._delivery_queued = True
    if isinstance(held, list):
        info._digest_settle_deliveries = []


def _notify_subagent_tab_gone(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    title: str,
    body: str,
    parent_key: str,
    slot_name: str,
) -> None:
    """Ring the bell for a completion whose parent tab is gone or closing."""
    assert self.dashboard_state is not None
    logger.info("Subagent %s: parent slot %s gone, notification only", info.id, slot_name)
    # Only notify when the tab is gone: an open tab already shows results in
    # the Activity panel and chat.
    self.dashboard_state.notify("subagent", title, body, meta=self._notif_meta(parent_key))
    # The notification is this route's whole delivery.
    info._report_delivered = True


async def _route_subagent_to_tab(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    *,
    announce: str,
    body: str,
    title: str,
    sub_meta: dict[str, Any],
    carried: tuple[str, ...],
    _flush_only: bool,
    _injection_slot: Any,
    _injection_slot_name: str,
    parent_key: str,
) -> None:
    """Deliver a completion into the dashboard tab that shows its parent.

    The completion becomes a queued card. An idle tab drains it into a turn at
    once; a busy one first waits for its turn, then queues it for that turn's
    drain. An announce whose every carried member the blocking spawn tool
    already returned is skipped; a tab that closed gets the bell. Every exit
    hands the completion off or settles it. A parent that is not dashboard-born
    (a cron job's tab, a channel conversation's) skips only once the turn that
    returned the result has landed, and parks it while that turn still holds
    the session, as the cron and channel routes do (``_inline_verdict``).
    """
    assert self.dashboard_state is not None
    _slot_name = _injection_slot_name
    # What a parked completion is routed with again at its turn's release, where
    # the route it then takes redacts it for its own surfaces.
    _announce_in, _body_in = announce, body
    # Route the result through _run_chat for full streaming, tool
    # call visibility, and proper lifecycle. A channel-born tab
    # runs on the channel's own session, so the turn's mirror
    # carries the reply back to the thread — the raw-injection path
    # below is for parents with no tab to stream into.
    # Redact LLM-generated output before any external surface
    announce, _ = redact_exfiltration_urls(announce)
    announce, _ = redact_credentials(announce)
    body, _ = redact_exfiltration_urls(body)
    body, _ = redact_credentials(body)

    if _injection_slot:
        # Whether the announce's members were returned inline, asked the way its
        # parent settles: at the release of the turn that received them, or, for a
        # dashboard-born parent, on the mark itself.
        _settles_at_release = settles_at_release(parent_key)

        def _inline_now() -> tuple[str, Any]:
            if _settles_at_release:
                return _inline_verdict(self.subagent_mgr, self.sessions, carried)
            if _delivered_inline(self.subagent_mgr, carried):
                return INLINE_CONFIRMED, None
            return INLINE_DELIVER, None

        # ── Fix 2 (B1): arm a one-shot post-fan-out synthesis turn ──
        # When this is the LAST outstanding sub-agent for the parent,
        # flag the slot so that once every completion has been
        # processed and the queue drains, _run_chat fires ONE
        # dedicated synthesis turn (see chat_runner drain/idle branch).
        # The fire gate drops it when one completion turn carried the
        # whole batch (slot._synthesis_completion_turns == 1).
        # Ordering guarantees running_agents_for == [] here on the last
        # agent (info.done set + _running_count decremented first).
        # IN MEMORY ONLY, cheapest first, and with no await: the arm
        # sits on the delivery path, so a store read here would queue
        # every completion behind the writer, and an await would let
        # the tab close or a sibling register under it. The store
        # half (a sibling the gate still holds) is the fire gate's
        # (chat_utils.synthesis_fire_verdict), which refuses while
        # anything is queued; the other completions re-arm.
        try:
            _mgr = self.subagent_mgr
            _arm_synthesis = (
                _mgr is not None
                and not _flush_only
                and _inline_now()[0] == INLINE_DELIVER
                and _mgr.running_agents_for(parent_key) == []
                and not _mgr.has_in_memory_pending_work_for(parent_key, exclude_id=info.id)
            )
        except Exception:
            _arm_synthesis = False  # error → don't arm (fail safe)
        if _arm_synthesis:
            # Every qualifying completion joins the armers, so an
            # inline-collected skip of one cannot cancel the synthesis
            # another still needs. A fresh arm starts a new set: ids left
            # from a synthesis that already ran or was dropped do not count.
            if not _injection_slot._pending_synthesis:
                _injection_slot._synthesis_armers.clear()
            _injection_slot._pending_synthesis = True
            _injection_slot._synthesis_armers.add(info.id)

        # ── Skip injection for blocking-tool-collected results ──
        # spawn_sub_agents (blocking MCP tool) already delivered
        # this result inline as a tool-call return value. Injecting
        # it again would trigger a redundant _run_chat turn whose
        # assistant response shadows any [OPTIONS:] buttons from the
        # synthesis message. Mark delivered and return.
        # The tool's mark-collected POST lands only after its reply
        # frame was written, which can be after this completion
        # arrives while the tab's turn is still running, so a busy
        # tab is waited on first and asked again -- see the second
        # check after the busy-wait below.
        def _skip_collected(when: str) -> bool:
            _verdict, _holding = _inline_now()
            if _verdict == INLINE_PENDING:
                # Returned to the turn still holding the session: parked until its
                # release settles it, never waited on inside ``_on_done``.
                self._park_inline(
                    _holding,
                    info,
                    route="tab",
                    announce=_announce_in,
                    body=_body_in,
                    title=title,
                    carried=carried,
                    parent_key=parent_key,
                    sub_meta=sub_meta,
                )
                return True
            if _verdict != INLINE_CONFIRMED:
                return False
            for aid in carried:
                _skip_inline_collected(_injection_slot, aid)
            logger.info(
                "Subagent %s: skipping %s (already collected inline by spawn_sub_agents)",
                info.id,
                when,
            )
            # The blocking tool's return value carried it.
            info._report_delivered = True
            return True

        if _skip_collected("injection"):
            return

        # Fix 2 (B1) race guard: count this completion as an
        # in-flight delivery from entry until it is handed off (turn
        # launched or queued). The synthesis fire-gate in chat_runner
        # requires this count to be zero, so a concurrently-finishing
        # sibling that is still awaiting the current turn (busy path)
        # can't let an earlier turn fire synthesis before this result
        # is delivered. try/finally so a CancelledError can't leak it.
        _injection_slot._subagent_deliveries_inflight += 1
        try:
            if _injection_slot_busy(_injection_slot):
                # Slot is busy (or an injection is dispatched but
                # not yet started) — wait for that task to finish,
                # then inject. No visible queue card.
                _current = _injection_slot.task
                if _current is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(_current),
                            timeout=INJECTION_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        pass  # Timed out waiting — slot still busy, will be queued below
                    except asyncio.CancelledError:
                        raise  # Don't swallow cancellation of this coroutine
                    except Exception:
                        pass  # Task failed — slot is now idle

                # Re-check: another injection may have claimed the slot
                # during the await above.
                if _injection_slot_busy(_injection_slot):
                    # Check inline-collected before queuing — if the
                    # blocking tool already handled this result, don't
                    # queue it for a later redundant turn.
                    if _skip_collected("queue"):
                        return
                    logger.info(
                        "Subagent %s: slot %s claimed by another injection, queuing",
                        info.id,
                        _slot_name,
                    )
                    # Each queued completion drains as its own turn
                    # (a system injection never merges), so it is
                    # one turn for the synthesis fire gate's count.
                    _injection_slot._synthesis_completion_turns += 1
                    # Bounded by the configured turn ceiling
                    # (chat_turn_timeout_secs, 14400s default):
                    # _run_chat's finally block drains slot._queue
                    # on any exit path.
                    # Carry the structured completion facts so the
                    # drained row is a card without re-parsing the
                    # prose; _start_next_queued_turn reads them.
                    _injection_slot.queue_append(
                        announce,
                        kind=SUBAGENT_COMPLETION_KIND,
                        meta={SUBAGENT_COMPLETION_META_KEY: sub_meta},
                    )
                    # Queuing is not delivery. The announce promises
                    # result paths the parent can read on demand, but
                    # it will not be in the parent's context until a
                    # turn drains it — a wait bounded only by the turn
                    # ceiling, so longer than the retention TTL. Owe
                    # the delivery tombstones to that drain instead of
                    # writing them now, or the reaper prunes
                    # result.txt while the promise is still queued
                    # and the parent is handed dead paths.
                    self._defer_queued_delivery(
                        _injection_slot, announce, info, flush_only=_flush_only
                    )
                    self.dashboard_state.push_slots_update()
                    logger.info("Subagent %s → queued in %s", info.id, _slot_name)
                    return

            # Slot is idle — re-check inline-collected (the
            # blocking tool's mark-collected POST follows its reply
            # frame's write, which precedes the end of its owning turn).
            if _skip_collected("injection after wait"):
                return

            # Slot is idle — hand the completion to the queue drain, the same
            # admission path a queued completion takes at a turn's end. Starting
            # `_run_chat` here directly would skip every gate the drain holds:
            # gateway shutdown, pending memory preparation, a Stop still being
            # resolved, a closing tab and a held sign-in queue. It would also
            # let this completion run ahead of an entry already parked in the
            # queue. A user message held while the children ran stays behind
            # it: the drain keeps holding user messages while a completion is in
            # flight. The drain owns the turn's provenance (`subagent`, from the
            # entry's kind) and settles the owed delivery marks once the model
            # consumes the announce, through the ledger
            # `_defer_queued_delivery` fills.
            #
            # The lock is taken before anything is queued. A tab that began
            # closing, or was replaced, while this waited for it would drop a
            # queued completion with the slot (a kinded entry is not saved), so
            # such a tab gets the bell instead, as one already gone does.
            _started = False
            _queue_id = ""
            async with _injection_slot._lock:
                if _injection_slot.is_closing or (
                    self.dashboard_state.get_slot(_injection_slot.key) is not _injection_slot
                ):
                    _tab_gone = True
                else:
                    _tab_gone = False
                    _injection_slot._synthesis_completion_turns += 1
                    _queue_id = _injection_slot.queue_append(
                        announce,
                        kind=SUBAGENT_COMPLETION_KIND,
                        meta={SUBAGENT_COMPLETION_META_KEY: sub_meta},
                    )
                    self._defer_queued_delivery(
                        _injection_slot, announce, info, flush_only=_flush_only
                    )
                    # A refusal leaves the card queued; the next drain (a
                    # turn's end, the memory-preparation drain, Run now)
                    # delivers it in order.
                    if idle_slot_admits_drain(self.dashboard_state, _injection_slot):
                        _injection_slot._turn_admission_reserved = True
                        try:
                            _started = await _start_next_queued_turn(
                                self.dashboard_state, _injection_slot
                            )
                        finally:
                            _injection_slot._turn_admission_reserved = False
            if _tab_gone:
                _notify_subagent_tab_gone(self, info, title, body, parent_key, _slot_name)
                return
            _task = _injection_slot.task
            if (
                _started
                and _task is not None
                and not any(item.get("id") == _queue_id for item in _injection_slot._queue)
            ):
                # A completion drains alone, so once its entry has left the
                # queue the turn just started is this completion's.

                def _on_inject_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
                    if not t.cancelled() and t.exception():
                        logger.error("Subagent injection _run_chat failed: %s", t.exception())
                        if self.subagent_mgr:
                            _reason = str(t.exception())
                            _reason, _ = redact_exfiltration_urls(_reason)
                            _reason, _ = redact_credentials(_reason)
                            self.subagent_mgr.notify_injection_failed(
                                info,
                                reason=_reason,
                            )

                _task.add_done_callback(_on_inject_done)
            self.dashboard_state.push_slots_update()
            logger.info(
                "Subagent %s → %s in %s",
                info.id,
                "turn started" if _started else "queued",
                _slot_name,
            )
        finally:
            _injection_slot._subagent_deliveries_inflight -= 1
            # While this count was up, a drain at another turn's end kept the
            # user messages queued (`_start_next_queued_turn`). When this
            # delivery started no turn of its own (it was collected inline, or
            # the tab is gone), nothing else would run them on an idle tab, so
            # the last delivery out drains it once.
            if (
                _injection_slot._subagent_deliveries_inflight == 0
                and _injection_slot._queue
                and idle_slot_admits_drain(self.dashboard_state, _injection_slot)
            ):
                _kick = asyncio.ensure_future(
                    drain_idle_parent_queue_for_stall(self.dashboard_state, parent_key)
                )
                self.dashboard_state._background_tasks.add(_kick)
                _kick.add_done_callback(self.dashboard_state._background_tasks.discard)
    else:
        _notify_subagent_tab_gone(self, info, title, body, parent_key, _slot_name)
    return


async def _inject_subagent_into_channel_parent(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    *,
    announce: str,
    body: str,
    title: str,
    carried: tuple[str, ...],
    parent_key: str,
) -> None:
    """Inject a completion into a channel or session parent that has no tab.

    The parent's ACP session takes the turn; only its synthesized reply goes to the
    conversation, through Slack or the channel's transport, then the bell rings.
    An announce whose every carried member the parent's blocking spawn tool
    already returned is not injected again.
    """
    # Parent with no tab to stream into — inject silently into the
    # parent's ACP session, then deliver only the synthesized reply
    # to the conversation. Make up to _MAX_INJECT_ATTEMPTS attempts;
    # only a timeout starts the next one (_inject_with_retry has its
    # own ACP-error retry inside each attempt). Slack keeps its
    # dedicated rich posting; every other channel namespace
    # (Telegram, Discord, …) delivers through the governed transport
    # ladder — its stored channel value is not a Slack channel id,
    # so posting it through the Slack client can never reach the
    # user. A parent with neither a channel namespace nor a Slack
    # thread (a taskrunner: session, say) is still injected, but its
    # reply goes to no channel.
    assert self.sessions is not None
    # A result returned inline to the turn still holding the parent's session
    # waits for that turn's release, parked, never inside ``_on_done``: the
    # release confirms the mark if the turn landed and revokes it otherwise.
    _verdict, _holding = _inline_verdict(self.subagent_mgr, self.sessions, carried)
    if _verdict == INLINE_PENDING:
        self._park_inline(
            _holding,
            info,
            route="channel",
            announce=announce,
            body=body,
            title=title,
            carried=carried,
            parent_key=parent_key,
        )
        return
    _namespace = channel_namespace_of(parent_key)
    _via_transport = bool(_namespace) and _namespace != SLACK_NAMESPACE
    _slack_thread = self.sessions.get_thread(parent_key)
    if _slack_thread is None and is_legacy_slack_key(parent_key):
        _slack_thread = parent_key
    _via_slack = _namespace == SLACK_NAMESPACE or bool(_slack_thread)
    if _via_transport:
        _inject_label = _namespace
    elif _via_slack:
        _inject_label = "Slack"
    else:
        _inject_label = "session"
    # Snapshot the transport reply link BEFORE the injection retry
    # loop: the timeout path's sessions.reset() evicts the session's
    # in-memory origin link, the first rung _channel_reply_link
    # tries, so resolving after a retry could miss the
    # conversation's own thread/forum target.
    _reply_link = self._channel_reply_link(parent_key) if _via_transport else None
    _injected = False
    # Asked before the first claim, so a result already returned starts no
    # session, and again after each claim, when a mark landed while the claim
    # waited out the parent's turn: by then that turn has released and settled
    # it. A returned result is skipped only once its turn landed; ``written``
    # means the bytes reached kiro-cli's pipe, and a stop, a reap or a failure
    # can still end that turn first, when the completion is delivered.
    _collected = _verdict == INLINE_CONFIRMED
    _inject_failure_reasons: list[str] = []
    _sleep_before_retry = False
    for _attempt in range(1, _MAX_INJECT_ATTEMPTS + 1):
        if _collected:
            break
        if _sleep_before_retry:
            await asyncio.sleep(2)
            _sleep_before_retry = False
        _acquired = False
        _footer_client = None
        try:
            logger.debug(
                "Subagent %s: %s injection attempt %d/%d into %s",
                info.id,
                _inject_label,
                _attempt,
                _MAX_INJECT_ATTEMPTS,
                parent_key,
            )
            _memory_store = await session_store_for_turn(self.ctx_builder, parent_key)
            client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
            _acquired = True
            _footer_client = client
            if _inline_verdict(self.subagent_mgr, self.sessions, carried)[0] == INLINE_CONFIRMED:
                _collected = True
                break
            _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
            if self.ctx_builder:
                # The completion is injected into the PARENT's
                # conversation, so it reads the parent session's silo,
                # resolved from that session's recorded binding. The
                # child's own store is not the answer here: this turn
                # continues the parent, and the reply it produces is
                # consolidated into the parent's store.
                msg, _ = await run_in_embed_pool(
                    self.ctx_builder.build_message,
                    announce,
                    is_new,
                    parent_key,
                    memory_store=_memory_store,
                    provider_type=_provider,
                    context_provider=client,
                    resumed=_resumed,
                    # The completion is the parent's own work coming back,
                    # not a person's request: it triggers no skill, as on
                    # the dashboard runner.
                    skill_trigger_text="",
                )
            else:
                msg = announce
            response = await asyncio.wait_for(
                self._inject_with_retry(info, client, msg, parent_key, _inject_label),
                timeout=INJECTION_TIMEOUT,
            )
            _injected = True  # LLM processed result; channel posting is best-effort

            # Persisted at the per-attempt level, NOT inside the
            # Slack branch below: a channel parent (Discord,
            # Telegram) delivers via the transport ladder and skips
            # that branch entirely, so persisting there dropped
            # every non-Slack subagent turn from replay. It still
            # runs BEFORE the Slack control is posted, so the
            # control's staleness token names this turn rather than
            # the one before it.
            # Persist BEFORE the control below invites
            # an answer to this turn: the token names
            # this session's last written row, so an
            # unwritten turn stamps the control with the
            # PREVIOUS turn's position and the first
            # click reads as already superseded.
            if self.conv_log and not (
                is_thread_temporary(parent_key) or is_thread_incognito(parent_key)
            ):
                try:
                    # Defense-in-depth: `announce` is composed from
                    # already-redacted parts plus identifiers such as
                    # `info.agent`; we re-redact before persisting to the
                    # dashboard replay (an external surface), mirroring the
                    # dashboard branch. `response` is fresh LLM output from
                    # stream_and_collect and is NOT yet redacted, so its
                    # redaction here is strictly required.
                    safe_announce, _ = redact_exfiltration_urls(announce)
                    safe_announce, _ = redact_credentials(safe_announce)
                    safe_response, _ = redact_exfiltration_urls(response or "")
                    safe_response, _ = redact_credentials(safe_response)
                    await save_conversation_turn_off_loop(
                        self.conv_log,
                        parent_key,
                        safe_announce,
                        safe_response,
                        source_thread=parent_key,
                        source_user="subagent",
                        agent=_get_agent_for_session(parent_key),
                    )
                except Exception:
                    logger.warning(
                        "Failed to persist subagent turn for %s",
                        parent_key,
                        exc_info=True,
                    )
            # Deliver only the LLM's synthesized response to the
            # parent's own conversation. Non-Slack channels go
            # through the governed transport ladder; on failure
            # the dashboard notification below still fires.
            if _via_transport and response:
                await self._deliver_channel_reply(parent_key, response, resolved_link=_reply_link)
            # Post only the LLM's synthesized response to Slack
            try:
                if response and _via_slack and self.slack and self._owner_id:
                    channel = (
                        self.sessions.get_channel(parent_key) if self.sessions else None
                    ) or await self.slack.open_dm(self._owner_id)
                    if channel:
                        # OPTIONS off the RAW text, then render: the
                        # tag is plain text, so extracting it after
                        # conversion made the controls hostage to
                        # to_slack_mrkdwn's 39,000-char truncation.
                        reply_text, options = extract_options(response)
                        for part in render_for_slack(reply_text):
                            await self.slack.post_message(channel, part, _slack_thread)
                        try:
                            elapsed = (
                                info.elapsed
                                if info.elapsed > 0
                                else (time.monotonic() - info.started)
                            )
                            footer_blocks, footer_text = build_timing_footer(
                                elapsed,
                                _footer_client,
                            )
                            if options:
                                _sub_token = await asyncio.to_thread(
                                    mint_options_token,
                                    self.dashboard_state,
                                    parent_key,
                                )
                                footer_blocks.extend(
                                    build_options_blocks(options, staleness_token=_sub_token)
                                )
                            _footer_ts = await self.slack.post_blocks(
                                channel,
                                footer_blocks,
                                footer_text,
                                _slack_thread,
                            )
                            self._remember_options(
                                parent_key,
                                channel,
                                _footer_ts,
                                options,
                                footer_blocks,
                                footer_text,
                            )
                        except Exception:
                            logger.debug(
                                "Failed to post timing footer for %s",
                                parent_key,
                                exc_info=True,
                            )
            except Exception:
                logger.exception(
                    "Subagent %s: Slack posting failed (injection succeeded)",
                    info.id,
                )

            logger.info("Subagent %s → %s session %s", info.id, _inject_label, parent_key)
            break
        except asyncio.TimeoutError:
            _inject_failure_reasons.append(
                f"attempt {_attempt} timed out after {int(INJECTION_TIMEOUT)}s"
            )
            logger.warning(
                "Subagent %s: %s injection attempt %d/%d timed out after %.0fs",
                info.id,
                _inject_label,
                _attempt,
                _MAX_INJECT_ATTEMPTS,
                INJECTION_TIMEOUT,
            )
            if _acquired:
                try:
                    await self.sessions.reset(parent_key)
                except Exception:
                    logger.debug(
                        "Failed to reset %s after channel injection timeout",
                        parent_key,
                        exc_info=True,
                    )
            if _attempt < _MAX_INJECT_ATTEMPTS:
                _sleep_before_retry = True
        except Exception as exc:
            _inject_failure_reasons.append(f"attempt {_attempt} failed: {exc}")
            logger.exception("Subagent %s %s injection failed", info.id, _inject_label)
            break
        finally:
            if _acquired:
                try:
                    # Only this injection's own prompt: after the timeout
                    # arm's reset the key may hold a successor whose live
                    # turn (the parent's next message) is not ours to cancel.
                    await self.sessions.cancel_current(
                        parent_key, owner_task=asyncio.current_task()
                    )
                except Exception:
                    logger.debug(
                        "Failed to cancel parent prompt for %s",
                        info.id,
                        exc_info=True,
                    )
                try:
                    # A person who writes during this hidden turn is queued by
                    # the channel's busy path, and that channel drains only at
                    # the tail of its own turns. Wake it here instead.
                    self.sessions.release(parent_key, wake_queue=True)
                except Exception:
                    logger.exception("Failed to release session %s", parent_key)

    if _collected:
        logger.info(
            "Subagent %s: skipping %s injection (already collected inline by spawn_sub_agents)",
            info.id,
            _inject_label,
        )
        # The blocking tool's return value carried it.
        info._report_delivered = True
    elif not _injected:
        _last_failure_reason = "; ".join(_inject_failure_reasons)
        _last_failure_reason, _ = redact_exfiltration_urls(_last_failure_reason)
        _last_failure_reason, _ = redact_credentials(_last_failure_reason)
        logger.error(
            "Subagent %s: all %d %s injection attempts failed: %s",
            info.id,
            _MAX_INJECT_ATTEMPTS,
            _inject_label,
            _last_failure_reason,
        )
        if self.subagent_mgr:
            self.subagent_mgr.notify_injection_failed(
                info,
                reason=_last_failure_reason,
            )
    # Dashboard notification
    if self.dashboard_state:
        self.dashboard_state.notify(
            "subagent",
            title,
            body,
            meta=self._notif_meta(parent_key),
        )
    return


async def _finish_subagent_cron_or_notify(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    *,
    announce: str,
    body: str,
    title: str,
    carried: tuple[str, ...],
    parent_key: str,
    injection_counted: bool = False,
) -> None:
    """Inject a completion into its cron parent, then ring the bell for any parent.

    A parent with no conversation to inject into is delivered by the bell alone,
    and an announce whose every carried member the parent's blocking spawn tool
    returned in a turn that has landed is not injected again. One whose turn
    still holds the session is parked until that turn releases it
    (``_inline_verdict``). ``injection_counted`` says the caller already counted
    this injection in ``_cron_injecting`` (a parked completion re-routed at its
    turn's release), so this route does not count it a second time.
    """
    # Cron parent — inject result back into the cron session.
    # Track pending injections to avoid resetting the session while
    # other subagents are queued behind the per-session semaphore.
    if parent_key.startswith("cron:"):
        verdict, holding = _inline_verdict(self.subagent_mgr, self.sessions, carried)
        if verdict == INLINE_PENDING and not injection_counted:
            self._park_inline(
                holding,
                info,
                route="cron",
                announce=announce,
                body=body,
                title=title,
                carried=carried,
                parent_key=parent_key,
            )
            return
        if not injection_counted:
            self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 0) + 1
        assert self.sessions is not None
        acquired = False
        cron_response: str | None = None
        # Asked before the claim, so a result already returned starts no session,
        # and again after it, when a mark landed while the claim waited out the
        # cron turn: by then that turn has released and settled it. A returned
        # result is skipped only once its turn landed; ``written`` means the bytes
        # reached kiro-cli's pipe, and a deadline, stop, reap or failure can still
        # end that turn first, when the completion is delivered.
        collected = verdict == INLINE_CONFIRMED
        try:
            if not collected:
                _memory_store = await session_store_for_turn(self.ctx_builder, parent_key)
                client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                acquired = True
                collected = (
                    _inline_verdict(self.subagent_mgr, self.sessions, carried)[0]
                    == INLINE_CONFIRMED
                )
            if collected:
                logger.info(
                    "Subagent %s: skipping cron injection (already collected inline by "
                    "spawn_sub_agents)",
                    info.id,
                )
                # The blocking tool's return value carried it.
                info._report_delivered = True
            else:
                _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                if self.ctx_builder:
                    # Same rule as the interactive injection above: the turn
                    # continues the PARENT conversation, so it reads the parent
                    # session's silo from that session's recorded binding.
                    msg, _ = await run_in_embed_pool(
                        self.ctx_builder.build_message,
                        announce,
                        is_new,
                        parent_key,
                        memory_store=_memory_store,
                        provider_type=_provider,
                        context_provider=client,
                        resumed=_resumed,
                        # Same rule as above: a completion triggers no skill.
                        skill_trigger_text="",
                    )
                else:
                    msg = announce
                cron_response = await asyncio.wait_for(
                    self._inject_with_retry(info, client, msg, parent_key, "cron"),
                    timeout=INJECTION_TIMEOUT,
                )
        except asyncio.TimeoutError:
            logger.error(
                "Subagent %s: cron injection timed out after %.0fs",
                info.id,
                INJECTION_TIMEOUT,
            )
            try:
                await self.sessions.reset(parent_key)
            except Exception:
                logger.debug(
                    "Failed to reset %s after cron injection timeout",
                    parent_key,
                    exc_info=True,
                )
            if self.subagent_mgr:
                self.subagent_mgr.notify_injection_failed(
                    info,
                    reason=f"injection timed out after {int(INJECTION_TIMEOUT)}s",
                )
        except Exception:
            logger.exception("Subagent %s cron injection failed", info.id)
            # Swallowed with no failure notice, so the record is the only
            # place that says the parent was never told.
            info._report_undelivered = True
        finally:
            if acquired:
                try:
                    # Same fence as the channel route: never the live turn
                    # of a successor that claimed the key after a reset.
                    await self.sessions.cancel_current(
                        parent_key, owner_task=asyncio.current_task()
                    )
                except Exception:
                    logger.debug(
                        "Failed to cancel parent prompt for cron %s", info.id, exc_info=True
                    )
                try:
                    self.sessions.release(parent_key)
                except Exception:
                    logger.exception("Failed to release session %s", parent_key)
            self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 1) - 1
            if self._cron_injecting[parent_key] <= 0:
                self._cron_injecting.pop(parent_key, None)
        if cron_response:
            cron_response, _ = redact_exfiltration_urls(cron_response)
            cron_response, _ = redact_credentials(cron_response)
            body = f"{body}\n\n{cron_response}"
            logger.info("Subagent %s → cron session %s", info.id, parent_key)
            # also deliver the synthesized response to the job's own
            # surfaces. honor the parent cron job's silent flag too:
            # info.silent is never set from the cron's silent setting for
            # spawn_run sub-agents, so a silent cron would otherwise still
            # post every subagent-completion turn.
            try:
                await self._deliver_cron_response(
                    parent_key,
                    cron_response,
                    silent=info.silent or self._cron_job_is_silent(parent_key),
                )
            except Exception:
                logger.exception(
                    "Subagent %s: failed to deliver cron response",
                    info.id,
                )
        await self._reset_cron_parent_when_idle(info, parent_key)

    # Dashboard notification
    if self.dashboard_state and not info.silent:
        self.dashboard_state.notify(
            "subagent",
            title,
            body,
            meta=self._notif_meta(parent_key),
        )
    if not parent_key.startswith("cron:"):
        logger.info("Subagent %s → notification only (parent=%s)", info.id, parent_key)
        # No conversation to inject into: the notification is this
        # route's whole delivery. A cron parent acknowledges in
        # ``_inject_with_retry`` instead, and only when it lands.
        info._report_delivered = True


async def _reset_cron_parent_when_idle(
    self: GatewayOrchestrator, info: SubagentInfo, parent_key: str
) -> None:
    """Reset a cron parent's session once nothing is left to deliver into it.

    The cleanup a cron turn's ``finally`` deferred while sub-agents ran, were
    queued or were being delivered: the last delivery into the session runs it,
    whether the cron route injected it or a tab showing the job took it.
    """
    assert self.sessions is not None
    # Reset only when no subagents running or QUEUED AND no
    # injections pending. Queued spawns (behind the concurrency /
    # stagger gate) have no SubagentInfo in `running` yet — a
    # sibling completing while the rest of the wave is still
    # queued must not reset the parent out from under them.
    still_running = self.subagent_mgr and (
        any(
            a.parent_session_key == parent_key and a.id != info.id
            for a in self.subagent_mgr.running
        )
        or await _subagent_queued_count(self.subagent_mgr, parent_key) > 0
    )
    still_injecting = self._cron_injecting.get(parent_key, 0) > 0
    if not still_running and not still_injecting:
        try:
            # Never under a live turn: a completion can land while the cron
            # turn is still answering (its spawn_sub_agents call returned the
            # result inline, so this route claimed nothing to wait it out),
            # and a busy reset would shut its provider down mid-reply. The reset
            # declines atomically then, and who holds the session decides the
            # rest. A permit held by a cron run's own task (_cron_runs_holding,
            # from before the run's first acquire to its end) is left to that
            # run's finalizer, which resets the session and retires the key.
            # Any other turn, on a tab that shows the job or a person's own,
            # keeps the session it is using, left to the idle sweep, and the
            # finished run's key is retired now.
            reset = await self.sessions.reset(parent_key, skip_if_busy=True)
            if not reset and self.sessions.is_busy(parent_key):
                holder = self.sessions.turn_holder(parent_key)
                if holder is not None and holder in self._cron_runs_holding:
                    logger.info(
                        "Cron session %s: last subagent done while its cron turn holds it; "
                        "left to that turn's finalizer",
                        parent_key,
                    )
                    return
                logger.info(
                    "Cron session %s: last subagent done while another turn holds it; "
                    "session left to that turn, key retired",
                    parent_key,
                )
            else:
                logger.info(
                    "Cron session %s: last subagent done, %s",
                    parent_key,
                    "session reset" if reset else "no session left to reset",
                )
            # Nothing holds the session now, or a turn other than the run's own
            # does: the reaper does not need the registered ephemeral key, and
            # a key left registered would keep the finished run live to job
            # ownership. A failed reset raises past this, so it leaves the key
            # registered (the ephemeral session may still be alive — the reaper
            # must be able to target it).
            # parent_key is "cron:{job_id}" (persistent) or
            # "cron:{job_id}:{run_id}" (ephemeral); job_id is the
            # second colon-separated segment in both cases. Exact-key
            # tracking means an older deferred reset removes only its
            # own key even after a newer run registers another one, and a
            # stable key the job's live run registered again (waiting for
            # the permit, or holding it) stays with that run, so the
            # reaper/ownership fence is never stripped from either. A key
            # the live run itself is done with (``finish_session_key``, set
            # as its engine released the session) is retired here.
            cron_svc = getattr(self, "cron_svc", None)
            if cron_svc is not None:
                parts = parent_key.split(":", 2)
                if len(parts) >= 2:
                    cron_svc.retire_finished_session_key(parts[1], parent_key)
        except Exception:
            logger.exception("Cron session %s: reset failed after last subagent", parent_key)
