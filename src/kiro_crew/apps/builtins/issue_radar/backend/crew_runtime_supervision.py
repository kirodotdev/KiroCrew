"""Supervision of running crews: every path that decides whether a crew takes another turn.

Trust reconciliation against a stop that lands at the same time, the signal wake,
the per-cycle watchdog, the slot-close and app-disable hooks, and the revocation of
a crew's execution and grants. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_runtime`.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew.apps import teardown
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.apps.teardown import register_slot_close_hook, register_slot_close_undo_hook
from kiro_crew.safety_override import safety_override

from . import crew_model, crew_store, provider, store
from .crew_runtime_prompt import compose_turn_prompt_async
from .crew_runtime_session import (
    APP_NAME,
    _autonudge_instance,
    _call_if_present,
    _current_crew,
    _record_model_hold,
    _rehydrate,
    _slot_key,
    _turn_model_items,
    autoapprove_scope,
    dispatch_crew_turn,
    ensure_crew_session,
    is_live,
    launch_crew,
    sync_trust,
)

logger = logging.getLogger("kirocrew.app.issue-radar")


#: Every crew's slot key starts with this. It is what identifies a crew session in
#: the gateway's slot registry when no crew RECORD is available to ask — which is
#: the case in :func:`revoke_crew_grants`, whose job includes catching a session
#: whose record is already gone.
_CREW_SLOT_PREFIX = "crew-"


def _is_crew_slot_key(key: str) -> bool:
    """Whether *key* names a CREW session, by id shape and not by prefix alone.

    The prefix on its own is a claim about a namespace this app does not own: a
    person may name an ordinary chat tab ``crew-notes``, and the app has no right
    to touch that tab's auto-nudge loop just because the string starts the same
    way. Validating the SUFFIX against the store's own id grammar is the same rule
    the record enumerator already follows (``is_crew_id(path.stem)`` rather than
    excluding known sibling filenames), applied to the one identifier that arrives
    from the slot registry instead of from disk.

    Where a slot object is in hand, ``_app == APP_NAME`` is the stronger check and
    is used in addition; a nudge LOOP carries no owning app, so this grammar is
    the only thing standing between "disable Issue Radar" and silently stopping an
    unrelated monitoring loop the user set up by hand.
    """
    if not str(key).startswith(_CREW_SLOT_PREFIX):
        return False
    return crew_store.is_crew_id(str(key)[len(_CREW_SLOT_PREFIX) :])


async def revoke_crew_execution(state: Any, crew: dict[str, Any], reason: str = "") -> bool:
    """Take away everything that could give this crew another turn. Idempotent.

    The inverse of :func:`sync_trust` plus the loop, and it exists as ONE function
    because stopping a crew means clearing two grants that the crew RECORD does not
    express: the autonudge loop is a live timer owned by another service, and
    ``slot._trust_scope`` plus its ``SafetyOverride`` grant are in-memory. Writing
    ``enabled``/``paused_reason``/``retired_at`` changes neither of them.

    That gap is a real window, not a tidiness point. Until something revokes, an
    idle timer that fires runs one more turn — auto-approved and unattended — on a
    crew a human just stopped. The watchdog notices, but it runs on the app's poll
    interval, so anything that only writes the record leaves that whole interval
    open. The pause and retire routes call this BEFORE they answer, which is what
    makes "stopped" true by the time the response lands; the watchdog's own call
    stays as the backstop for a crew stopped by a direct record edit or by a
    restart.

    Best-effort by design: the record is the durable truth and it is already
    written when a route gets here, so a failure to reach the in-memory grants must
    not turn a successful pause into a 500. Returns whether anything was actually
    revoked, which is what tests assert on.
    """
    _note_revocation(str(crew.get("id") or ""))
    slot_key = _slot_key(crew)
    scope = autoapprove_scope(str(crew.get("id") or ""))
    revoked = False
    slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
    # ``scope_remaining_secs`` and not ``is_scope_active``: the latter EXPIRES a
    # lapsed grant and emits a ``scope_expired`` SEL event, so probing with it here
    # would forge an expiry record for a grant this call is about to revoke anyway.
    if safety_override().scope_remaining_secs(scope) > 0:
        revoked = True
    safety_override().deactivate_scope(scope)
    if slot is not None:
        if getattr(slot, "_trust_scope", ""):
            slot._trust_scope = ""
            revoked = True
        # Also clear an interactive grant a human may have clicked onto this slot.
        # "Stop this crew" has to mean stopped, and unlike :func:`sync_trust` — which
        # runs unprompted every cycle and so must leave a human's choice alone — this
        # runs only because a human or the watchdog decided the crew must not run.
        if getattr(slot, "_trust", False):
            slot._trust = False
            revoked = True
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is not None:
        try:
            loop = svc.get_by_slot(slot_key)
            if loop is not None and loop.active:
                await svc.update(loop.id, active=False)
                revoked = True
        except Exception:  # pragma: no cover - cleanup must not raise
            logger.warning(
                "issue-radar crew %s: could not deactivate its loop (%s)",
                crew.get("id"),
                reason or "stopped",
                exc_info=True,
            )
    if revoked:
        logger.info(
            "issue-radar crew %s: execution revoked (%s)",
            crew.get("id"),
            reason or "stopped",
        )
    return revoked


#: Per-crew revocation counters. Bumped ON the loop by every revocation of that
#: crew; read on the loop before a reconciliation hops to a worker and compared on
#: the loop after it. A change means a stop of THIS crew landed during the mint, and
#: the grant the worker produced must not stand. Keyed by crew id rather than kept
#: as one process-wide counter, because an unrelated crew's pause must not de-trust
#: a crew that is mid-turn -- that would park a live unattended crew on an approval
#: prompt for a cycle. Process memory, like the grant itself.
_revoke_generation: dict[str, int] = {}

#: Set by :func:`revoke_crew_grants` (the app-disable revoker) and cleared by the
#: watchdog once it observes the app enabled again. Exists because the disable hook
#: runs BEFORE ``installed.json`` says disabled: in that window the gate reads on,
#: and a reconciliation that starts there has already sampled the bumped generation
#: -- so neither of the other two sources can see the stop. The latch is the
#: in-process record of it. Never read on its own for permission; it only ever
#: DENIES.
_disabling = False


def _note_revocation(crew_id: str) -> None:
    """Record that a stop of *crew_id* happened. Called ON the loop by both revokers."""
    _revoke_generation[crew_id] = _revoke_generation.get(crew_id, 0) + 1


def _trust_inputs(slot: Any, crew: dict[str, Any]) -> tuple[bool, bool]:
    """One worker-thread hop: ``(app_enabled, granted)``.

    The grant has three sources of truth and the crew record carries only two of
    them. ``unattended`` and liveness are on the record; whether the APP is on lives
    in ``installed.json``, and :func:`sync_trust` alone cannot see it. Read together
    here so the reconciliation decides from all three at once, off the loop.
    """
    enabled = is_app_enabled(APP_NAME)
    return enabled, (sync_trust(slot, crew) if enabled else False)


async def _reconcile_trust(state: Any, slot: Any, crew: dict[str, Any]) -> bool:
    """Assign the crew's grant from every source of truth, honouring a stop that
    completes during the assignment. Returns whether the crew is trusted AFTER.

    The only way this module may grant from an async path. Three things decide the
    grant and each is guarded here in the way its failure mode demands:

    * **The app gate.** ``revoke_crew_grants`` (app disable) clears grants and reads
      no crew record, so ``sync_trust`` would happily re-mint on a disabled app --
      the record still says unattended and live. The gate is read in the SAME hop
      as the mint (:func:`_trust_inputs`), so an app already off never mints. It is
      NOT re-read on the loop afterwards: that would parse ``installed.json`` on the
      gateway's only thread, and it is not needed -- a disable that completes during
      the hop runs ``revoke_crew_grants``, which bumps this crew's generation, and
      the generation compare below is what catches it.
    * **The record.** ``sync_trust`` is the assignment: ``unattended AND is_live``.
    * **A stop during the hop.** ``_revoke_generation[crew id]`` is sampled on the
      loop before the hop and compared on the loop after. A change means this crew
      was revoked while the worker was deciding, so whatever it minted is torn down.
      Both reads sit on the single-threaded loop with no ``await`` between them and
      the mint's completion, which is what makes the comparison sound.

    A disable that completed BEFORE the sample is the first bullet's job and needs
    no counter -- the gate itself is the record of that stop. A disable that lands
    AFTER the sample, whether during the hop or after it, is the third bullet's:
    its revoker bumps the generation and the compare sees it. One ordering slips
    between them: the disable HOOK runs before ``installed.json`` is written, so a
    reconciliation that starts after the hook's bump but before the flag write sees
    a bumped-and-sampled generation and an enabled gate. ``_disabling`` -- latched
    by the hook's revoker, released by the watchdog on re-enable -- is the record of
    that in-flight stop, and it is checked on both sides of the hop like the
    generation. Neither of the three reads a file on the loop.
    """
    crew_id = str(crew.get("id") or "")
    if _disabling:
        revoke_crew_grants(state)
        return False
    before = _revoke_generation.get(crew_id, 0)
    enabled, granted = await asyncio.to_thread(_trust_inputs, slot, crew)
    if not enabled or _disabling:
        # Off before the hop, or a disable began during it. Idempotent against a
        # disable whose synchronous half already ran.
        revoke_crew_grants(state)
        return False
    if _revoke_generation.get(crew_id, 0) != before:
        await revoke_crew_execution(state, crew, "stopped during trust reconciliation")
        return False
    return bool(granted)


async def wake_crew(
    state: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    reason: str = "",
    root: Path | None = None,
    key: provider.RepoKey | None = None,
) -> bool:
    """Give the crew a turn NOW because a signal moved. Returns whether a turn started.

    The wake proper is :func:`_wake_crew_body`; what this wrapper adds is the ONE
    exit every path out of it goes through. The auto-approve grant is minted from a
    record read before an ``await`` (``sync_trust``, off the loop), so any path that
    returns without re-checking can leave a grant the pause route had ALREADY
    revoked standing on a crew a human stopped — a resurrected grant, not merely a
    late one. Guarding each ``return`` individually does not close that: it cannot
    cover the exception path at all, and every instance found so far was a return
    nobody had listed yet.

    So the check lives in a ``finally``, where a ``return`` added anywhere in the
    body cannot bypass it: re-read the record and revoke unless it comes back
    readable AND live. ``revoke_crew_execution`` is idempotent and best-effort, so
    the paths that already revoked pay nothing and a failure here cannot turn a
    successful wake into an exception.

    The reconciliation is itself an ``await``, so a stop can complete inside it and
    the worker can then mint from a record read before the stop. That is not closed
    by ordering reads -- the mint is always last and always a suspension point -- so
    it is closed by making the revocation visible: :func:`_reconcile_trust` compares
    ``_revoke_generation`` across the hop and tears the grant down if a revocation
    ran meanwhile. A stop whose revocation lands after that comparison is not one
    this wake raced -- nothing here mints after it -- and the grant is re-checked on
    every approval rather than sampled once.

    RECONCILED THROUGH ``sync_trust``, never through a hand-written condition. The
    grant's predicate is ``unattended AND is_live`` (:func:`sync_trust`), so a guard
    that checks liveness alone is blind to half of it: turning ``unattended`` off is a
    governance downgrade the record expresses, and a crew that is still enabled walks
    straight past a liveness check. Handing the fresh record back to the function that
    mints the grant is what keeps the two in step — it is an assignment, so it revokes
    and re-establishes as the record dictates, and no copy of its predicate can drift
    from it.

    The guarantee, stated as the invariant it is: **when ``wake_crew`` returns, the
    crew's grant matches the record as the store held it after all of the wake's
    work** — so no grant survives a stop, or an ``unattended`` downgrade, that was
    already written when this read ran.
    """
    try:
        return await _wake_crew_body(state, owner, repo, crew, reason, root, key)
    finally:
        current = await _current_crew(owner, repo, crew, root)
        if current is None or not is_live(current):
            await revoke_crew_execution(state, current or crew, "stopped while waking")
        else:
            # Live, but the record may still have downgraded ``unattended``. Look the
            # slot up rather than carry it out of the body: this must run on every
            # exit, including the ones that never resolved a slot at all.
            slot = state.get_slot(_slot_key(current)) if hasattr(state, "get_slot") else None
            if slot is not None:
                await _reconcile_trust(state, slot, current)


async def _wake_crew_body(
    state: Any,
    owner: str,
    repo: str,
    crew: dict[str, Any],
    reason: str = "",
    root: Path | None = None,
    key: provider.RepoKey | None = None,
) -> bool:
    """Give the crew a turn NOW because a signal moved. Returns whether a turn started.

    Two writes, both needed. The armed loop's message is refreshed so an idle-timer
    fire that lands later carries the CURRENT snapshot instead of the one composed
    at launch; and the prompt is dispatched immediately, because the whole point of
    the sweep is that the crew does not wait out an idle gap after CI turns red.
    ``enqueue_or_run_prompt`` queues instead of racing when the crew is mid-turn.

    The ``crew`` argument is the sweep's snapshot and a pause writes only to the
    store, so liveness is re-read from the store before the grant and again before
    the dispatch. Without them a pause landing anywhere in this function is
    invisible here and the crew spends one auto-approved turn after being stopped.

    Called only through :func:`wake_crew`, whose ``finally`` is what makes those
    checks cover the paths this function returns on without reaching the dispatch.
    """
    slot = state.get_slot(_slot_key(crew))
    if slot is None:
        slot = await _rehydrate(state, str(crew.get("slot_key") or ""))
    if slot is None:
        logger.info(
            "issue-radar crew %s: no session to wake (%s)", crew.get("id"), reason or "signal"
        )
        return False
    at_grant = await _current_crew(owner, repo, crew, root)
    if at_grant is None:
        # No readable record: FAIL CLOSED. The grant is a governance control, so an
        # unreadable record is not permission to keep granting from a snapshot whose
        # liveness nothing can confirm. ``wake_crew``'s finally revokes.
        logger.warning(
            "issue-radar crew %s: no readable record, wake refused (%s)",
            crew.get("id"),
            reason or "signal",
        )
        return False
    crew = at_grant
    await _reconcile_trust(state, slot, crew)
    prompt = await compose_turn_prompt_async(slot, owner, repo, crew, root, key)
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is not None:
        loop = svc.get_by_slot(slot.key)
        if loop is not None:
            try:
                await svc.update(loop.id, message=prompt)
            except Exception:  # pragma: no cover - refresh is best-effort
                logger.debug("issue-radar: nudge refresh failed", exc_info=True)
    # Read here, BEFORE the dispatch-time re-read below, so that re-read stays the
    # last await before the turn. The model is chosen from it synchronously there.
    turn_items = await _turn_model_items(slot, owner, repo, crew, root)
    if getattr(slot, "running", False):
        # DROPPED, not queued — the same call autonudge's own fire path makes, and
        # for a stronger reason here: a queued prompt can carry the whole brief, so
        # a crew that stayed busy across three sweeps would come back to three
        # stacked copies of its own instructions. Nothing is lost by dropping it.
        # The loop's message was just refreshed with the current snapshot, and the
        # crew reconciles EVERY open item against the six signals at the top of each
        # turn anyway — the wake buys latency, it does not carry information.
        logger.info(
            "issue-radar crew %s: mid-turn, wake dropped (%s)",
            crew.get("id"),
            reason or "signal",
        )
        return False
    # The last check before the turn, and a re-read rather than a look at ``crew``
    # because composing the prompt and refreshing the loop message are both awaits:
    # the record can have moved since the grant.
    at_dispatch = await _current_crew(owner, repo, crew, root)
    if at_dispatch is None or not is_live(at_dispatch):
        logger.info(
            "issue-radar crew %s: stopped while waking, no turn dispatched (%s)",
            crew.get("id"),
            reason or "signal",
        )
        return False
    # Re-assign trust from THAT record before the turn starts, rather than dispatching
    # on the grant minted from the earlier one. It moves in both directions and that
    # is the point: a crew whose ``unattended`` flag went off, or which was paused and
    # resumed inside this wake, gets the grant the current record calls for instead of
    # the one an older read did. :func:`wake_crew`'s finally re-runs the same
    # reconciliation after the dispatch, which is what covers a record that moves
    # again in between. A revocation that lands INSIDE this reconciliation is seen
    # by it (see :func:`_reconcile_trust`), and then there is no turn to dispatch:
    # an unattended crew whose grant was just taken must not be started untrusted.
    if not await _reconcile_trust(state, slot, at_dispatch) and at_dispatch.get("unattended"):
        logger.info(
            "issue-radar crew %s: stopped during grant, no turn dispatched (%s)",
            crew.get("id"),
            reason or "signal",
        )
        return False
    # The model this turn runs on, from the ledger read above and the record just
    # re-read. Synchronous, and nothing awaits between it and the dispatch, so no
    # other turn can start on the slot in between.
    if turn_items is not None:
        crew_model.apply_turn_model(state, slot, at_dispatch, turn_items)
    tagged = f"[crew wake: {reason}]\n{prompt}" if reason else prompt
    started = dispatch_crew_turn(state, slot, tagged)
    _call_if_present(state, "push_slots_update")
    # A hand pick the switch just saw goes on the record, as the watchdog pass
    # does, so the crew page (which shows only a recorded hold) can offer the way
    # out. After the dispatch, so nothing awaits between the switch and the turn.
    # Not gated on a choice having been made: a hold seen earlier whose write did
    # not land is retried here too.
    await _record_model_hold(owner, repo, at_dispatch, root)
    logger.info(
        "issue-radar crew %s woken (%s): turn %s",
        crew.get("id"),
        reason or "signal",
        "started" if started else "queued",
    )
    return bool(started)


# ── the watchdog (one pass per poll cycle) ──────────────────────────────────


#: Why a crew is paused when the user closes its chat tab. Stored in
#: ``paused_reason``, so the roster explains the stop instead of showing a crew
#: that is enabled and inexplicably idle.
DISMISSED_PAUSE_REASON = "session closed"


async def _on_slot_closed(slot_key: str, root: Path | None = None) -> None:
    """The user dismissed a crew's chat tab: PAUSE that crew.

    Pausing rather than silently un-arming, because a paused crew is a state the
    product already has a first-class representation for — ``is_live`` is already
    False for it, the sweep's existing revocation branch already takes its trust
    away, the roster already shows ``paused_reason``, and the existing pause route
    already resumes it. An autonomous worker that stops has to SAY it stopped.

    Reached only from a deliberate ✕ (see ``apps.teardown.notify_slot_closed``),
    never from idle archival — so this cannot mistake a crew that was merely quiet
    for one the user asked to stop.
    """
    found = await asyncio.to_thread(_crew_for_slot_key, slot_key, root)
    if found is None:
        return
    owner, repo, crew, scope = found
    if crew.get("retired_at") or crew.get("paused_reason"):
        # Already stopped, and for a reason someone else recorded. Overwriting it
        # would replace a specific explanation with a generic one.
        return
    await asyncio.to_thread(
        crew_store.set_crew_paused,
        owner,
        repo,
        str(crew.get("id") or ""),
        True,
        DISMISSED_PAUSE_REASON,
        scope,
    )
    logger.info("issue-radar crew %s paused: its session was closed (%s)", crew.get("id"), slot_key)


async def _on_slot_close_undone(slot_key: str, root: Path | None = None) -> None:
    """The dismissal did not happen: RESUME the crew this hook paused.

    Guarded on the reason string, which is the only evidence that THIS hook is what
    stopped the crew. ``_on_slot_closed`` refuses to overwrite an existing
    ``paused_reason``, so a crew paused for any other cause never got touched and
    must not be resumed here — un-pausing that would turn a failed tab close into a
    worker restart nobody asked for, which is worse than the state being repaired.
    """
    found = await asyncio.to_thread(_crew_for_slot_key, slot_key, root)
    if found is None:
        return
    owner, repo, crew, scope = found
    if crew.get("retired_at") or crew.get("paused_reason") != DISMISSED_PAUSE_REASON:
        return
    await asyncio.to_thread(
        crew_store.set_crew_paused,
        owner,
        repo,
        str(crew.get("id") or ""),
        False,
        "",
        scope,
    )
    logger.info(
        "issue-radar crew %s resumed: the close that paused it failed (%s)",
        crew.get("id"),
        slot_key,
    )


def _crew_for_slot_key(
    slot_key: str, root: Path | None = None
) -> tuple[str, str, dict[str, Any], Path | None] | None:
    """Find ``(owner, repo, crew)`` for a slot key. Blocking — call in a thread.

    A slot key carries no repo, so this walks the connected repos. Retired crews are
    included: the caller decides what to do about one, and reporting "no such crew"
    for a crew that plainly exists reads as a bug.

    EVERY PROVIDER, not just one. The watchdog registers the close hook once per
    swept repo and passes that repo's PROVIDER root, and the registry holds one hook
    per app — so the last sweep's provider won, and closing the tab of a crew on any
    other provider found nothing: the crew stayed live and the next sweep re-armed
    its auto-approved session. A slot key is global, so its lookup has to be too.
    The given root is still tried first (tests and explicit callers pass a data root
    directly); the provider sweep is a fallback, so it costs an extra pass only when
    a close does not match, which is the rare path.
    """
    if not slot_key:
        return None
    for scope in _lookup_scopes(root):
        for entry in store.list_connected_repos(scope):
            owner = str(entry.get("owner") or "")
            repo = str(entry.get("repo") or "")
            if not owner or not repo:
                continue
            for crew in crew_store.list_crews(owner, repo, root=scope, include_retired=True):
                if str(crew.get("slot_key") or "") == slot_key:
                    # The SCOPE comes back too: the pause has to be written where
                    # the crew actually lives. Writing it under the hook's own root
                    # would file the pause in a different provider's store and leave
                    # the real crew running.
                    return owner, repo, crew, scope
    return None


def _lookup_scopes(root: Path | None) -> list[Path | None]:
    """The data roots to search for a crew, in order: the given one, then every
    connected repo's own provider root.

    Derived per entry rather than assumed, because a mixed GitHub/GitLab install
    keeps each provider's records under its own root and no single root sees both.
    """
    scopes: list[Path | None] = [root]
    try:
        for entry in store.list_connected_repos(None):
            # Only pass what the entry actually carries: `provider_root` defaults
            # to github/github.com, and a legacy entry omits both — passing "" would
            # bypass those defaults and resolve a root nothing lives under.
            kw: dict[str, str] = {}
            if entry.get("provider"):
                kw["provider"] = str(entry["provider"])
            if entry.get("host"):
                kw["host"] = str(entry["host"])
            scope = store.provider_root(root=None, **kw)
            if scope not in scopes:
                scopes.append(scope)
    except Exception:  # pragma: no cover - defensive; never block a close
        logger.debug("issue-radar: could not enumerate provider roots", exc_info=True)
    return scopes


def install_slot_close_hook(root: Path | None = None) -> None:
    """Register the dismissal hook. Idempotent; safe to call every cycle.

    Called from the watchdog rather than once at import, for the same reason the
    watchdog re-establishes trust: the registry is process memory, so a gateway
    restart empties it and a one-shot registration would leave the ✕ silently
    ignored for the rest of that process's life.
    """
    register_slot_close_hook(APP_NAME, partial(_on_slot_closed, root=root))
    # Registered TOGETHER with the close hook, never separately: the pause is only
    # safe to take because it can be taken back, so a process that can pause but
    # not resume is the half-applied state this pair exists to avoid.
    register_slot_close_undo_hook(APP_NAME, partial(_on_slot_close_undone, root=root))


async def on_app_disabled(state: Any, app_name: str = "") -> int:
    """The operator switched Issue Radar off: STOP the crews now, in the request.

    THE FIRST LINE. Turning an app off is a human saying stop, and it has to mean
    stopped by the time the disable returns — not stopped within a poll interval.
    The sweep in ``watch.py`` suspends crews too, but it wakes on
    ``POLL_INTERVAL_SEC``, so on its own it left up to a full minute in which an
    already-armed nudge could fire a whole auto-approved turn for an app the
    operator had just disabled. Same reasoning, and the same shape, as pause and
    retire revoking inline before they answer (:func:`revoke_crew_execution`).

    Registered rather than called: core must be able to stop an app's work without
    importing the app, so this is handed to the hook registry in
    ``apps/teardown.py`` by :func:`install_app_disable_hook`, exactly as the
    dismissal hook is. ``app_name`` is accepted and unused so the registry may pass
    the disabled app's name; the registration is already per-app, so there is
    nothing to filter on here.

    Ordering note for a reader tracing the window: core's teardown runs BEFORE the
    ``enabled`` flag is written, so this revokes while the app is still nominally
    on. That is deliberate and it is why nothing here consults ``is_app_enabled`` —
    a hook that waited for the flag would reintroduce the gap it closes.
    """
    return await suspend_crews(state)


def install_app_disable_hook(state: Any) -> bool:
    """Ask to be told the moment the app is disabled. Returns whether a seam exists.

    Idempotent and safe every cycle, for the reason
    :func:`install_slot_close_hook` documents: the registry is process memory, so a
    gateway restart empties it.

    Feature-detected with ``getattr`` rather than an import, because this app must
    keep working against a core build that has no such registry — and there the
    ``watch.py`` sweep is the only suspension there is, which is the pre-hook
    behaviour rather than a regression. The caller reports a missing seam once (see
    ``watch._watch_loop``); this function stays quiet so the per-cycle
    re-registration cannot turn a known limitation into a log flood.
    """
    # ``getattr`` rather than importing the name directly: the lookup happens per
    # call, so a gateway whose teardown module predates this seam degrades to the
    # ``watch.py`` sweep instead of failing at import time.
    register = getattr(teardown, "register_app_disable_hook", None)
    if register is None:
        return False
    register(APP_NAME, partial(on_app_disabled, state))
    return True


async def watchdog_cycle(
    state: Any,
    owner: str,
    repo: str,
    crews: list[dict[str, Any]],
    root: Path | None = None,
    key: provider.RepoKey | None = None,
) -> None:
    """Re-establish what the process does not persist. Zero API calls, zero LLM.

    Three things, every cycle, for the same reason ``auto_research``'s watchdog does
    them: they are all in-memory state that a restart drops.

    * **Trust.** A crew's auto-approve grant lives in memory and expires on its own
      (:func:`sync_trust`), so an unattended crew silently loses it on restart, and
      on a watchdog that stopped — which is the point — and the next tool call parks
      it in an approval prompt nobody is watching. Trust needs a slot to hold the
      scope key, so a crew whose slot the gateway does not hold is rehydrated
      first — an armed loop fires against the slot key regardless, and it must land
      on a trusted session.
    * **The loop.** A crew whose autonudge loop is missing or deactivated has no
      clock at all; the sweep still wakes it on a signal, but it would never pick up
      new work again. Re-arming here makes "enabled" mean enabled.
    * **Revocation.** A crew that was disabled, retired or paused while its slot was
      resident keeps its trust until something takes it away. The BACKSTOP, not the
      first line: pause and retire revoke inline before they answer, and this cycle
      catches what they cannot — a record edited directly, and a restart that
      re-arms a loop for a crew that is not live.
    * **Both hooks.** The dismissal hook and the app-disable hook are process
      memory, so a restart empties them. Re-registering here is what keeps a ✕ and
      an app disable from going quiet for the rest of a process's life.
    """
    global _disabling
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    install_slot_close_hook(root)
    install_app_disable_hook(state)
    # The sweep runs this only while the app reads ENABLED, so reaching it means any
    # disable in flight when the latch was set has long since written its flag and
    # the operator has re-enabled the app. Releasing here, and nowhere else, is what
    # keeps the latch a one-way denial until the store agrees the app is back.
    _disabling = False
    for crew in crews:
        slot_key = _slot_key(crew)
        slot = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
        if slot is None and is_live(crew):
            # A live crew with no resident slot: a restart, or a tab someone
            # closed. Rehydrate HERE and not only in the ``loop is None`` branch
            # below, because a PERSISTED loop fires against the slot key whether
            # or not the gateway still holds it — so without this the crew's first
            # post-restart turn runs with its grant gone and an unattended
            # crew parks on an approval nobody is there to answer. Rehydration
            # first, so the crew keeps its own transcript (and its brief).
            slot = await _rehydrate(state, slot_key)
        # UNCONDITIONALLY, and not only on the branch that rehydrated. The roster
        # was read before this pass and every crew ahead of this one in the loop
        # awaited, so a resident crew's snapshot is just as stale as a rehydrated
        # one's — and everything below decides on it: the grant, the launch, and
        # switching the crew's clock back on.
        current = await _current_crew(owner, repo, crew, root)
        if current is None or not is_live(current):
            await revoke_crew_execution(state, crew, "not live")
            continue
        crew = current
        if slot is not None:
            await _reconcile_trust(state, slot, crew)
        if svc is not None:
            loop = svc.get_by_slot(slot_key)
            if loop is None:
                # No loop for a live crew: either it has never been launched or a
                # restart lost it. Launching is idempotent on the slot key. The slot
                # is KEPT: the trailing reconciliation below must reach a session
                # this pass just created, or its grant is the one thing in the pass
                # nothing re-checks.
                slot = await launch_crew(state, owner, repo, crew, root, key)
            else:
                if slot is None:
                    # Loop but still no slot — nothing on disk to rehydrate from (a
                    # crew armed and then never given a turn). ``ensure_crew_session``
                    # creates the session and establishes trust the same way launch
                    # does, without re-arming a loop that already exists.
                    slot = await ensure_crew_session(state, owner, repo, crew)
                if not loop.active:
                    await svc.update(loop.id, active=True)
        # The model the next idle-timer turn runs on: the loop fires on its own
        # clock, and this pass is what keeps the slot's model in step with the
        # ledger between wakes. A triage turn that moved an item into an acting
        # phase is ESCALATED: the work-model turn is dispatched below rather than
        # left for the idle timer, so a red CI found on a cheap turn is acted on
        # within one poll, not one idle gap.
        escalate = False
        turn_items = await _turn_model_items(slot, owner, repo, crew, root)
        if turn_items is not None:
            before = getattr(slot, "model", "") or ""
            moved = crew_model.apply_turn_model(state, slot, crew, turn_items)
            escalate = (
                moved
                and before == crew_model.triage_model(crew)
                and crew_model.has_acting_item(turn_items)
            )
        # A hand pick this pass (or a wake) saw goes on the record, so a restart
        # keeps it.
        await _record_model_hold(owner, repo, crew, root)
        # The pass itself awaits — the grant, the launch, the re-arm — so read once
        # more and undo it if the operator stopped the crew while it ran.
        # ``revoke_crew_execution`` is the exact inverse of what this body just
        # established (the grant, the loop, the interactive flag), so this closes the
        # window rather than narrowing it: no branch above can leave a stopped crew
        # holding a grant or a live clock until the next cycle. The straight-line
        # shape above, in place of the earlier ``continue``s, is what makes it total.
        current = await _current_crew(owner, repo, crew, root)
        if current is None or not is_live(current):
            await revoke_crew_execution(state, current or crew, "stopped mid-cycle")
        elif slot is not None:
            # Still live, but ``unattended`` may have gone off mid-pass. Same rule as
            # the wake's exit guard: reconcile through the function that mints the
            # grant, never through a copy of half its predicate -- and through the
            # generation check, so a stop landing inside the hop wins.
            await _reconcile_trust(state, slot, current)
            if escalate:
                # Through the wake, so the escalated turn gets every check a
                # signal's turn does: liveness re-read, trust, the mid-turn drop.
                await wake_crew(
                    state, owner, repo, current, "an item needs the work model", root, key
                )


def revoke_crew_grants(state: Any) -> int:
    """Take every resident crew session's auto-approve grant away. Returns how many.

    SYNCHRONOUS ON PURPOSE, and that is the whole security property: it holds no
    ``await``, so once it is entered nothing else runs on the event loop until every
    grant is gone. Its caller can therefore promise that a crew is untrusted by the
    time the caller's own first suspension point is reached — which is what lets
    :func:`on_app_disabled` close the disable window rather than narrow it.

    Works entirely from IN-MEMORY state — the resident slots — and reads nothing from
    disk. It walks slots rather than records so it also catches a crew session whose
    record was deleted while the session was live, which a record-driven walk would
    miss. Idempotent.
    """
    global _disabling
    _disabling = True
    cleared = 0
    for key, slot in list((getattr(state, "_slots", None) or {}).items()):
        if not str(key).startswith(_CREW_SLOT_PREFIX) or getattr(slot, "_app", "") != APP_NAME:
            continue
        # Every crew slot this walk reaches counts as revoked, granted or not: a
        # reconciliation mid-hop for it must see the disable whatever the slot
        # carried when the walk arrived. The id comes from the key because the
        # record may be gone.
        _note_revocation(str(key)[len(_CREW_SLOT_PREFIX) :])
        touched = False
        # The scope key is read off the SLOT rather than rebuilt from the record:
        # this walk exists precisely to catch a crew session whose record is gone.
        scope = str(getattr(slot, "_trust_scope", "") or "")
        if scope:
            safety_override().deactivate_scope(scope)
            slot._trust_scope = ""
            touched = True
        if getattr(slot, "_trust", False):
            slot._trust = False
            touched = True
        if touched:
            cleared += 1
    if cleared:
        logger.info("issue-radar disabled — cleared trust on %d crew session(s)", cleared)
    return cleared


async def suspend_crews(state: Any) -> int:
    """Turning the app off must STOP the crews. Returns how many slots were cleared.

    Two things keep a crew going once armed, and neither is expressed by any record
    an app-enabled flag could speak for: the crew's auto-approve grant, keyed off
    its slot, and its autonudge loop, which fires regardless of any app's enabled
    flag. Without this, a disabled Issue Radar keeps running unattended,
    auto-approved turns.

    TWO callers, and which is which matters:

    * :func:`on_app_disabled` — the FIRST LINE, awaited inside the disable request
      itself, so "stopped" is true by the time the operator's disable returns.
    * ``watch._poll_once``'s disabled branch — the BACKSTOP, for the disables this
      module can never be told about: ``kirocrew app disable`` in another process,
      an ``installed.json`` edited on disk, and a gateway restart that re-armed
      loops for an app that is off. It runs on the poll interval by nature.

    GRANTS BEFORE LOOPS, and this order is load-bearing rather than tidy. The grant
    is the thing that lets a turn act unattended, and deactivating loops is the only
    step here that awaits — so doing loops first would hand the event loop back with
    the grants still live, and an armed timer that fired in that gap would get its
    auto-approved turn from the very call that was stopping it. Reversing these two
    statements reopens the window this function exists to close.

    Reads nothing from disk: a disabled app must stay silent, and that includes not
    walking the connected-repo config on every one of its idle cycles. Idempotent,
    as the precedent in ``auto_research`` is.
    """
    if state is None:
        return 0
    cleared = revoke_crew_grants(state)
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is not None:
        for loop in svc.list_all():
            if not _is_crew_slot_key(loop.slot_key):
                continue
            if loop.active:
                try:
                    await svc.update(loop.id, active=False)
                except Exception:  # pragma: no cover - cleanup must not raise
                    logger.warning(
                        "issue-radar: could not deactivate crew loop %s on disable", loop.id
                    )
    return cleared
