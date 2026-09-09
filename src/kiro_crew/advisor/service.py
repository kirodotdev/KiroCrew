"""Advisor service: parent-session registry and effective enablement.

The service is the single owner of advisor state for live sessions. When the
effective setting is off it is inert: no observer is created, nothing is
buffered, and no reviewer runtime exists (grounding: architecture constraint
that disabled means zero cost).
"""

from __future__ import annotations

import asyncio
import logging
import time

from kiro_crew.advisor import composition
from kiro_crew.advisor.composition import AdvisorDispatcher, render_update_prompt
from kiro_crew.advisor.guard import EmissionGuard
from kiro_crew.advisor.observation import AdvisorObserver, ObservationUpdate
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.config.sections import normalize_agent_model
from kiro_crew.llm_helpers import parse_llm_json
from kiro_crew.validation import MODEL_ID_RE

logger = logging.getLogger(__name__)

#: Boundary reasons. Reset, compaction and `/clear` re-prime the observer in
#: place (its epoch turns over so erased or rewritten history never
#: resurfaces); close disposes it.
BOUNDARY_RESET = "reset"
BOUNDARY_COMPACTION = "compaction"
BOUNDARY_CLEAR = "clear"
BOUNDARY_CLOSE = "close"

#: Per-session override values. ``inherit`` defers to the global setting.
OVERRIDE_INHERIT = "inherit"
OVERRIDE_ON = "on"
OVERRIDE_OFF = "off"


def resolve_effective_enabled(global_enabled: bool, override: object) -> bool:
    """The session's effective advisor enablement.

    ``on``/``off`` win over the global default in both directions; anything
    else -- ``inherit``, an empty value, or a value written by a future
    version this build does not know -- defers to the configured default
    rather than silently enabling.
    """
    if override == OVERRIDE_ON:
        return True
    if override == OVERRIDE_OFF:
        return False
    return bool(global_enabled)


class AdvisorService:
    """Registry of per-parent-session advisor observers.

    v1 scope: enablement gate and observer lifecycle. Reviewer runtime
    attachment, guard, and delivery policy compose on top of this registry.
    """

    def __init__(self, enabled: bool = False, *, reviewer_available: bool = False) -> None:
        self._enabled = enabled
        self._observers: dict[str, AdvisorObserver] = {}
        self.reviewer_model: str = ""
        # The reviewer is a kiro-cli agent. Default CLOSED: it opens only when
        # configure_from_config positively confirms the kiro backend, so a
        # stale per-session `on` cannot attach a reviewer in the window before
        # configuration (or under any other selected harness). Composed into
        # EVERY enablement decision.
        self.reviewer_available: bool = reviewer_available
        #: Whether the strict sandbox can mask credentials on this host --
        #: kiro-cli auto-approves builtin reads, so that mask is the reviewer's
        #: read boundary. Probed once, off-loop, by the gateway's post-bind
        #: configure task (``None`` until then); ``False`` keeps the reviewer
        #: unavailable with a visible reason instead of a degradation at the
        #: first review.
        self.sandbox_available: bool | None = None
        #: Minimum seconds between IN-PROGRESS reviews per session; the final
        #: update always reviews. Guards reviewer spend against checkpoint
        #: storms (a live run produced 20 reviews on one turn without it).
        self.review_min_interval_secs: float = 20.0
        self._last_review_at: dict[str, float] = {}
        #: Bumped on every reset/compaction/rewrite/opt-out/terminal boundary
        #: for a session; the pump uses it to tell a raced boundary apart from
        #: an ordinary next-turn re-prime (which never bumps it).
        self._boundary_gen: dict[str, int] = {}
        #: Each live observer's override source (inherit|on) -- a global
        #: disable detaches inherited observers immediately but keeps the
        #: explicitly opted-in ones, so it must know which is which.
        self._override_source: dict[str, str] = {}
        self._pool: object | None = None
        #: The reviewer model the live pool was built for; a config change to
        #: a different model must replace the pool, not keep the stale one.
        self._pool_model: str = ""
        #: Bumped by every configure_from_config apply. The startup task
        #: snapshots this before its threaded config load and discards its
        #: (now stale) snapshot when a live PATCH applied in between.
        self._config_epoch: int = 0
        self._guards: dict[str, EmissionGuard] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def attach(
        self, parent_session_key: str, override: str = OVERRIDE_INHERIT
    ) -> AdvisorObserver | None:
        """Attach an observer for a parent session at the current boundary.

        ``override`` is the slot's persisted ``inherit|on|off`` selection,
        composed with the global default by ``resolve_effective_enabled``.
        Returns None when the effective setting is off. Enabling mid-session
        starts from the current boundary: the fresh observer holds no
        historical records.
        """
        if not self.reviewer_available or not resolve_effective_enabled(self._enabled, override):
            # Opt-out is honored at every turn, not only at first attach:
            # drop the live observer and its guard so observation stops now.
            # The generation bump exists to discard a review that raced the
            # revocation, so it happens ONLY when an observer was actually
            # revoked -- under the default-off advisor every chat turn lands
            # here, and an unconditional insert would grow the registry by
            # one entry per distinct session for the gateway's lifetime.
            if self._observers.pop(parent_session_key, None) is not None:
                self._guards.pop(parent_session_key, None)
                self._override_source.pop(parent_session_key, None)
                self._boundary_gen[parent_session_key] = (
                    self._boundary_gen.get(parent_session_key, 0) + 1
                )
            return None
        self._override_source[parent_session_key] = override
        observer = self._observers.get(parent_session_key)
        if observer is None:
            observer = AdvisorObserver(parent_session_key=parent_session_key, turn_id="")
            self._observers[parent_session_key] = observer
            # A session opted in under a globally-off default still needs a
            # reviewer pool; configure_from_config deliberately constructs
            # nothing while disabled, so bind lazily at first effective use.
            if self._pool is None:
                _ensure_pool_bound(self)
        else:
            # A sealed epoch means the previous turn finished: re-prime so
            # this turn's records land instead of raising, preserving any
            # completed update the pump has not consumed yet. A new turn also
            # resets dedupe, so a blocker repeated on turn 2 is not suppressed
            # by turn 1's guard state.
            if observer.epoch_completed:
                self._guards.pop(parent_session_key, None)
            observer.begin_turn()
        return observer

    def observer_count(self) -> int:
        return len(self._observers)

    def set_reviewer_pool(self, pool: object) -> None:
        """Bind the reviewer pool (real or fake) used by the pump."""
        self._pool = pool

    async def pump_async(self, state: object, slot: object) -> None:
        """Drain the slot's pending observation and run one bounded review.

        The full asynchronous path: drain -> render -> pool review ->
        envelope extraction -> guarded, severity-routed dispatch. Total: a
        disabled service, an absent observer, an empty drain, a missing
        pool, and a failed review all end the pump quietly -- the primary
        turn never waits on, or breaks because of, the advisor.
        """
        # Gate on the ATTACHED observer, never the global flag: attach_for_turn
        # already composed the global default with the per-session override, so
        # an observer's existence IS the enablement decision -- a session opted
        # "on" under a global-off default reviews, and an opted-out session
        # (observer dropped at attach) does not.
        # Pinned lookup: a slot object rebound since attach fronts another
        # conversation and must not pump (and steer) its observer.
        observer = _observer_for(slot)
        pool = getattr(self, "_pool", None)
        if observer is None or pool is None:
            return
        session_key_early = _slot_session_key(slot)
        # Snapshot the boundary generation BEFORE the await below: acquisition
        # can be slow (a cold reviewer spawn), and an opt-out or reset landing
        # during it detaches/invalidates this observer.
        acquire_gen = self._boundary_gen.get(session_key_early, 0)
        # Acquire the reviewer session BEFORE the destructive drain: a runtime
        # spawn failure here must neither crash the pump task nor consume the
        # update -- the observer keeps it and the next pump retries.
        try:
            await pool.acquire_session(session_key_early)
        except Exception:
            logger.warning(
                "advisor reviewer session acquire failed for %s; update retained",
                session_key_early,
                exc_info=True,
            )
            return
        # Revalidate IMMEDIATELY after the await, before any drain: when the
        # user opts out (or a boundary fires) during acquisition, the
        # evidence in this observer loses its authorization to leave the
        # session -- draining it into pool.review would disclose it.
        if (
            self._observers.get(session_key_early) is not observer
            or self._boundary_gen.get(session_key_early, 0) != acquire_gen
            or _slot_session_key(slot) != session_key_early
        ):
            # The third arm catches an in-place slot REBIND (cron/workflow
            # swapping linked_session_key) landing during the cold spawn: the
            # slot now fronts a different conversation whose key was never
            # acquired, and the recorded evidence belongs to the old one.
            logger.info(
                "advisor pump aborted: session boundary or rebind during "
                "reviewer acquisition for %s",
                session_key_early,
            )
            return
        update: "ObservationUpdate | None" = observer.take_completed()
        if update is None:
            last = self._last_review_at.get(session_key_early, 0.0)
            if time.monotonic() - last < self.review_min_interval_secs:
                return  # throttled: the next checkpoint or terminal catches up
            update = observer.drain_update()
        if update is None:
            return
        session_key = _slot_session_key(slot)
        advisor_update_id = f"{session_key}:{update.epoch}:{update.seq}"
        payload: dict[str, object] = {
            "prompt": render_update_prompt(update),
            # The reviewer session's evidence tools read the OBSERVED slot's
            # tree; empty when the slot has no project (gateway cwd fallback).
            "work_dir": str(getattr(slot, "project", "") or ""),
        }
        review_gen = self._boundary_gen.get(session_key, 0)
        review_epoch = update.epoch

        def _authorized() -> bool:
            # LIVE authorization, re-evaluated by the pool after its
            # semaphore wait and immediately before transmission: the
            # observer must still be the attached one, no boundary/opt-out
            # may have bumped the generation, and the slot must still front
            # this conversation.
            return (
                self._observers.get(session_key) is observer
                and self._boundary_gen.get(session_key, 0) == review_gen
                and _slot_session_key(slot) == session_key
            )

        payload["_authorized"] = _authorized
        self._last_review_at[session_key] = time.monotonic()
        raw = await pool.review(session_key, payload)
        if raw is None:
            return

        # A reset/compaction/opt-out that RACED the review must discard its
        # stale result -- but a same-conversation next turn must NOT, or the
        # final review the pump already took gets dropped. So gate on the
        # boundary GENERATION (bumped only by a true boundary, never by a
        # next-turn re-prime) and on the observer still being the live one.
        # The SLOT's identity can move under the review too: a cron/workflow
        # rebind swaps `linked_session_key`, so the slot now fronts a different
        # conversation while the old key's observer and generation are both
        # unchanged. Recompute the effective key -- advice reviewed for one
        # conversation must never persist or stage into its replacement.
        # The same predicate is handed to the dispatcher and re-evaluated
        # before EVERY note: a blocker steer awaits the running turn, so a
        # revocation can land between two notes of one review.
        def still_authorized() -> bool:
            return (
                self._observers.get(session_key) is observer
                and self._boundary_gen.get(session_key, 0) == review_gen
                and _slot_session_key(slot) == session_key
            )

        if not still_authorized():
            logger.info(
                "advisor review discarded: session boundary or slot rebind "
                "(%s -> %s) during review",
                session_key,
                _slot_session_key(slot),
            )
            return
        # Same conversation, but the NEXT turn began while the review ran: the
        # advice describes a finished turn, so steering it into the successor
        # would interrupt work it never observed. Preserve instead (card +
        # staged context) -- the advice still reaches the next prompt.
        steer_allowed = observer._epoch == review_epoch
        if steer_allowed:
            guard = self._guards.setdefault(session_key, EmissionGuard())
        else:
            # LATE review (the next turn already began): its dedupe/budget
            # state describes a finished turn. Storing it in the live guard
            # would let a stale note's text suppress the SAME blocker raised
            # for the current turn. Use a throwaway epoch-local guard --
            # the preserve path never steers, so no interruption accounting
            # is lost.
            guard = EmissionGuard()
        dispatcher = AdvisorDispatcher(guard=guard)
        # Bare JSON, a fenced block or an object embedded in prose all decode;
        # anything else reaches the dispatcher as-is and is recorded as a
        # degradation there, uniformly with a decoded-but-invalid envelope.
        envelope = parse_llm_json(raw) if isinstance(raw, str) else None
        if envelope is None:
            envelope = raw
        outcomes = await dispatcher.dispatch(
            state,
            slot,
            envelope,
            advisor_update_id=advisor_update_id,
            steer_allowed=steer_allowed,
            authorized=still_authorized,
        )
        # One INFO line per review: the only production signal that says
        # whether advice flowed, was suppressed, or degraded. A silent
        # reviewer and a broken dispatcher look identical without it.
        logger.info(
            "advisor review %s: %s",
            advisor_update_id,
            outcomes if outcomes else "no notes (or all suppressed)",
        )

    def notify_boundary(self, parent_session_key: str, reason: str) -> None:
        """Join a parent lifecycle boundary. Total: never raises.

        Reset, compaction and clear start a new observation epoch so pending
        records and dedupe state cannot cross a rewritten conversation; close
        disposes the observer. Gated on OBSERVER PRESENCE, not the global
        flag: a session opted in while the global default is off still has
        live state that must not survive its boundaries. Unknown sessions
        are no-ops -- a lifecycle path must never fail because of the
        advisor.
        """
        observer = self._observers.get(parent_session_key)
        if observer is None:
            if reason == BOUNDARY_CLOSE:
                # A session that opted out (or was revoked) mid-life has no
                # observer but may still own detached-state entries (the
                # revocation's generation bump). Its close reclaims them.
                self._boundary_gen.pop(parent_session_key, None)
                self._last_review_at.pop(parent_session_key, None)
                self._override_source.pop(parent_session_key, None)
            return
        # Every real boundary bumps the generation so a review racing it is
        # discarded (distinct from an ordinary next-turn re-prime).
        self._boundary_gen[parent_session_key] = self._boundary_gen.get(parent_session_key, 0) + 1
        if reason == BOUNDARY_CLOSE:
            self._observers.pop(parent_session_key, None)
            self._guards.pop(parent_session_key, None)
            # Reclaim EVERY per-session dict: a closed session never comes
            # back under this key, and entries left behind accumulate for
            # the gateway's lifetime.
            self._last_review_at.pop(parent_session_key, None)
            self._boundary_gen.pop(parent_session_key, None)
            self._override_source.pop(parent_session_key, None)
            self._schedule_pool_release(parent_session_key)
            return
        # Epoch-scoped by default: an unrecognized future reason re-primes
        # rather than silently keeping stale state. The guard resets with
        # the epoch -- dedupe and cooldown must not suppress a repeated
        # blocker across a reset or compaction.
        observer.begin_epoch()
        self._guards.pop(parent_session_key, None)

    def dispose_all(self) -> None:
        """Drop every observer and guard (gateway shutdown/recycle).

        Also schedules the reviewer pool's shutdown so the shared subprocess
        dies with the gateway instead of orphaning. Total and non-blocking.
        """
        self._observers.clear()
        self._guards.clear()
        self._last_review_at.clear()
        self._boundary_gen.clear()
        self._override_source.clear()
        pool = self._pool
        if pool is not None:
            self._pool = None
            self._spawn_bg(getattr(pool, "shutdown", None))

    def _schedule_pool_release(self, parent_session_key: str) -> None:
        """Release the parent's reviewer session without blocking the caller."""
        pool = self._pool
        if pool is None:
            return
        release = getattr(pool, "release_session", None)
        if release is None:
            return
        self._spawn_bg(release, parent_session_key)

    @staticmethod
    def _spawn_bg(fn: object, *args: object) -> None:
        """Run an async pool operation as a fire-and-forget task. Total."""
        if fn is None:
            return

        try:
            asyncio.get_running_loop().create_task(fn(*args))  # type: ignore[operator]
        except RuntimeError:  # no running loop (sync context/tests)
            logger.debug("pool op skipped: no running event loop")
        except Exception:
            logger.debug("pool op scheduling failed", exc_info=True)


_service: AdvisorService | None = None


def get_advisor_service() -> AdvisorService:
    """The process-wide advisor service (disabled until configured on)."""
    global _service
    if _service is None:
        _service = AdvisorService(enabled=False)
    return _service


def configure_from_config(cfg: object) -> None:
    """Apply the ``advisor.*`` config section to the process-wide service.

    Total: junk or missing sections leave the service in its current state
    (disabled by default). Called at gateway startup and config reload.
    """
    advisor = getattr(cfg, "advisor", None)
    if advisor is None:
        return
    service = get_advisor_service()
    service._config_epoch += 1
    service._enabled = bool(getattr(advisor, "enabled", False))
    # The reviewer model is the "advisor" ROLE pin (agent.role_models is the
    # only sanctioned place to pin a model for a class of work); "auto" or an
    # absent pin normalizes to "" -- the runtime's default model, as before.
    role_models = getattr(getattr(cfg, "agent", None), "role_models", None)
    pinned = role_models.get("advisor", "") if isinstance(role_models, dict) else ""
    model = normalize_agent_model(pinned) if isinstance(pinned, str) else ""
    if model and not MODEL_ID_RE.match(model):
        # The role gate admits display-only canonical keys the runtime's
        # constructor rejects; binding one would fail every review. Fall back
        # to the runtime default and say so, rather than degrade silently.
        logger.warning("advisor role pin %r is not a runtime model id; using the default", model)
        model = ""
    service.reviewer_model = model
    # The packaged reviewer spec is a kiro-cli agent definition (kiro-cli tool
    # names, resolved by ``--agent``), so the reviewer runtime is kiro-cli only
    # in v1. Binding it under another selected harness would spawn a process
    # the operator never chose: refuse once, loudly, and detach EVERY observer
    # -- a per-session `on` is no authorization to use an unselected harness.
    backend = getattr(getattr(cfg, "agent", None), "acp_backend", ACP_BACKEND_KIRO)
    service.reviewer_available = (
        backend == ACP_BACKEND_KIRO and service.sandbox_available is not False
    )
    if not service.reviewer_available:
        if service._enabled:
            logger.warning(
                "advisor: reviewer unavailable (agent.acp_backend=%r, sandbox_available=%r); "
                "the reviewer runs on the kiro-cli agent backend only and needs the strict "
                "sandbox's credential mask -- advisor stays disabled",
                backend,
                service.sandbox_available,
            )
        service._enabled = False
        for key in list(service._observers):
            service._observers.pop(key, None)
            service._guards.pop(key, None)
            service._override_source.pop(key, None)
            service._boundary_gen[key] = service._boundary_gen.get(key, 0) + 1
    # Pool binding follows enablement: a disabled advisor constructs NOTHING
    # -- a default-off feature must not construct eagerly at startup --
    # and disabling live unbinds + schedules the pool's shutdown so the
    # settings toggle governs the whole lifecycle. The pool itself spawns no
    # process until an enabled session's first review.
    if service._enabled or service._pool is not None:
        # (Re)bind when there is no pool, or when the reviewer model changed
        # under us — a stale pool would run the old model while rows are
        # labeled with the new one. A pool bound for an opted-in session
        # under a globally-off default follows model changes the same way.
        _ensure_pool_bound(service)
    if not service._enabled:
        # Disabling must stop INHERITED observation immediately -- the next
        # checkpoint fires before the next attach, so waiting for attach's
        # opt-out pass would keep feeding the reviewer after the operator
        # said stop. Explicit per-session `on` survives: that session chose
        # observation independently of the global default.
        for key in [k for k, src in list(service._override_source.items()) if src != OVERRIDE_ON]:
            service._observers.pop(key, None)
            service._guards.pop(key, None)
            service._override_source.pop(key, None)
            service._boundary_gen[key] = service._boundary_gen.get(key, 0) + 1
    if not service._enabled and not service._observers and service._pool is not None:
        pool = service._pool
        service._pool = None
        service._pool_model = ""
        service._spawn_bg(getattr(pool, "shutdown", None))


def apply_override_change(parent_session_key: str, override: str) -> None:
    """React to a per-session override write IMMEDIATELY. Total.

    Setting the override to an effectively-off value mid-turn must stop
    observation NOW: the live observer would otherwise keep feeding the
    running turn's checkpoints to the reviewer (session data egress + spend)
    until the next attach re-resolves the override. Detaches the observer and
    guard, bumps the boundary generation so an in-flight review is discarded
    on completion, and releases the reviewer session. Effectively-on values
    are inert here -- attach_for_turn owns enablement.
    """
    service = get_advisor_service()
    if service.reviewer_available and resolve_effective_enabled(service._enabled, override):
        # Still effectively on -- but the AUTHORIZATION SOURCE moved (e.g.
        # `on` -> `inherit` under a globally-on default). A later global
        # disable detaches inherited observers by this record; leaving the
        # stale source would keep session data flowing past that disable.
        if parent_session_key in service._observers:
            service._override_source[parent_session_key] = override
        return
    if service._observers.pop(parent_session_key, None) is None:
        return
    service._guards.pop(parent_session_key, None)
    service._override_source.pop(parent_session_key, None)
    service._boundary_gen[parent_session_key] = service._boundary_gen.get(parent_session_key, 0) + 1
    service._schedule_pool_release(parent_session_key)


def notify_hard_kill(parent_session_key: str) -> None:
    """Invalidate any in-flight review for *parent_session_key*. Total.

    A hard kill (second Stop press) discards everything in flight, reviewer
    advice included. The steer maps are cleared by the stop handler; a review
    already inside ``pool.review`` is invisible to those maps, so this bumps
    the session's boundary generation -- the pump's post-review check then
    discards the result instead of persisting a card and staging context the
    user explicitly threw away. The observer stays: the SESSION survives a
    hard kill, only the in-flight work is discarded.
    """
    service = get_advisor_service()
    observer = service._observers.get(parent_session_key)
    if observer is None:
        # No observer means nothing is in flight or recorded for this
        # session; inserting a generation entry here would grow the
        # registry for every hard-killed session under a disabled advisor.
        return
    service._boundary_gen[parent_session_key] = service._boundary_gen.get(parent_session_key, 0) + 1
    # The generation bump only discards reviews ALREADY in flight: a pump
    # scheduled after the kill snapshots the NEW generation, so evidence and
    # sealed updates recorded before the kill would still drain and review.
    # The user threw that work away -- reset the epoch (records + queued
    # final updates) and drop the guard's window with it.
    observer.begin_epoch()
    service._guards.pop(parent_session_key, None)


def _ensure_pool_bound(service: "AdvisorService") -> None:
    """Bind (or rebind) the reviewer pool for the configured model.

    Idempotent for an unchanged model; a changed model replaces the pool and
    schedules the old one's shutdown so review processes never outlive their
    configuration. Shared by config (re)application and the lazy bind at a
    session's first effective use.
    """
    if service._pool is not None and service._pool_model == service.reviewer_model:
        return
    old_pool = service._pool
    try:
        service.set_reviewer_pool(composition.build_reviewer_runtime(service.reviewer_model))
        service._pool_model = service.reviewer_model
        if old_pool is not None:
            service._spawn_bg(getattr(old_pool, "shutdown", None))
    except Exception:
        logger.warning("advisor pool bind failed", exc_info=True)


def _slot_session_key(slot: object) -> str:
    """The session key *slot*'s turns run on -- the advisor's registry key.

    The dashboard's own resolution (``chat_utils.effective_session_key``):
    boundaries are fired with that key, so the observer must be registered
    under the same spelling.
    """
    # circular import: dashboard.chat_runner imports this module's hooks, so
    # the dashboard is resolved at call time.
    from kiro_crew.dashboard.chat_utils import effective_session_key

    return effective_session_key(slot)  # type: ignore[arg-type]


def attach_for_turn(slot: object) -> AdvisorObserver | None:
    """Attach (or fetch) the slot's observer for a starting turn.

    Composes the global default with the slot's persisted override. Cheap and
    inert when the effective setting is off: one dict lookup, no buffering.
    """
    service = get_advisor_service()
    override = getattr(slot, "advisor_override", OVERRIDE_INHERIT)
    session_key = _slot_session_key(slot)
    if not session_key:
        return None
    # Capture BEFORE attach: attach() runs begin_turn(), which re-primes a
    # sealed epoch (epoch_completed becomes False) while preserving the
    # queued final update -- so the post-attach flag cannot distinguish a
    # normal seal from a crash. Only a turn that was NEVER sealed is the
    # crash case that warrants a fresh epoch.
    prior = service._observers.get(session_key)
    was_sealed = prior is None or prior.epoch_completed or not prior._turn_id
    observer = service.attach(session_key, override=override)
    if observer is not None:
        # Parent-turn identity for every record this turn produces; refreshed
        # each attach so a multi-turn observer never reports a stale turn.
        new_turn_id = f"turn-{getattr(slot, '_turn_generation', 0)}"
        if observer is prior and observer._turn_id != new_turn_id and not was_sealed:
            # The previous turn died without a terminal (provider failure,
            # crash): its epoch never sealed, so begin_turn was a no-op and
            # the stale evidence would be attributed to -- and could steer --
            # THIS turn. A changed identity on an unsealed epoch means the
            # old turn is over; start clean -- the emission guard too, or the
            # dead turn's admitted blocker and cooldown would silence the
            # recovery turn's advice (the terminal paths pop it the same way).
            observer.begin_epoch()
            service._guards.pop(session_key, None)
        observer._turn_id = new_turn_id
    # Pin the key this turn attached under: every later checkpoint looks the
    # observer up by the slot's CURRENT key, and a cron/workflow rebind swaps
    # `linked_session_key` mid-turn -- the slot object then fronts another
    # conversation whose observer must not receive this turn's evidence.
    setattr(slot, "_advisor_turn_key", session_key)
    return observer


def _observer_for(slot: object) -> AdvisorObserver | None:
    service = get_advisor_service()
    session_key = _slot_session_key(slot)
    if getattr(slot, "_advisor_turn_key", session_key) != session_key:
        return None  # rebound since attach: drop rather than misroute
    return service._observers.get(session_key)


def observe_tool_result(slot: object, tool_name: str, payload: str) -> None:
    """Record a completed tool result for the slot's observer, if any.

    Total: called from the chat runner's hot event loop, so a broken observer
    degrades silently rather than breaking the turn.
    """
    observer = _observer_for(slot)
    if observer is None:
        return
    try:
        observer.record_tool_result(tool_name, payload)
    except Exception:  # advisor must never break the primary turn
        logger.debug("advisor observe_tool_result failed", exc_info=True)


def observe_segment(slot: object, text: str) -> None:
    """Record a finalized assistant segment for the slot's observer, if any."""
    observer = _observer_for(slot)
    if observer is None:
        return
    try:
        observer.record_segment(text)
    except Exception:
        logger.debug("advisor observe_segment failed", exc_info=True)


def complete_turn(slot: object, stop_reason: str | None = None, synthetic: bool = False):
    """Emit the turn's final observation update, if an observer is attached."""
    observer = _observer_for(slot)
    if observer is None:
        return None
    try:
        return observer.complete(stop_reason=stop_reason, synthetic=synthetic)
    except Exception:
        logger.debug("advisor complete_turn failed", exc_info=True)
        return None


def schedule_pump(state: object, slot: object) -> None:
    """Fire-and-forget one review pump for the slot, when warranted.

    Called from the chat runner's event loop. Cheap pre-checks (enabled,
    observer present, pool bound) run synchronously so a disabled advisor
    costs two dict lookups and no task; the pump itself runs as a background
    task the primary turn never awaits.
    """

    service = get_advisor_service()
    if getattr(service, "_pool", None) is None:
        return
    if _observer_for(slot) is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(service.pump_async(state, slot))
    except RuntimeError:  # no running loop (sync test context)
        return
    tasks = getattr(state, "_background_tasks", None)
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
