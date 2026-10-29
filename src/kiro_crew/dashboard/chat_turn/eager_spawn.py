"""Starting a dashboard slot's session before its first message: the eager spawn and the resume
prefetch, their admission, backoff and TTL, the slot-binding snapshot a handshake is checked
against, and the shutdown's cancel of every pending start."""

from __future__ import annotations

import asyncio
import functools
import time
from typing import TYPE_CHECKING, Any

from kiro_crew import resource_status
from kiro_crew.agent_capabilities import CapabilityError
from kiro_crew.agent_sdk.tool_search import resume_takes_tool_search_replay
from kiro_crew.config.loader import normalize_agent_model
from kiro_crew.security import redact_and_truncate
from kiro_crew.session import SessionEndingError
from kiro_crew.session_capabilities import CapabilityStartupError
from kiro_crew.start_priority import StartPriority

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        KiroCrewConfig,
        SessionClosingError,
        SpeculativeResumeRefused,
        _arm_generation,
        _armed_prefetches,
        _ChatSlot,
        _eager_spawn_sem,
        effective_session_key,
        logger,
        record_agent_selection,
        refresh_materialized_agents,
        resolve_agent_bindings,
        resolve_session_agent_bindings,
        select_provider_backend,
        shutdown_event,
        subprocess_executor,
    )
    from kiro_crew.dashboard.chat_turn.app_agent_binding import (
        _recover_app_agent_binding,
        app_agent_binding_applies,
    )
    from kiro_crew.dashboard.chat_turn.crew_log_attribution import _slot_predecessor_store
    from kiro_crew.dashboard.chat_turn.model_fallback import _app_allocation_kwargs
    from kiro_crew.dashboard.chat_turn.session_reset import _consume_pending_reset
    from kiro_crew.dashboard.chat_turn.turn_context import _require_session_memory_assignment

# Debounce before a speculative spawn. Absorbs rapid consecutive signals
# (slot create immediately followed by a project set, or a user re-picking
# the project) so only the settled state spawns a session.
_EAGER_SPAWN_DEBOUNCE_SECS = 1.5


# Background starts of a slot whose agent keeps failing to start. Each failure
# spawns and tears down a whole process tree, and the signals that schedule an
# eager spawn (focus, reconnect, slot create, reset) fire again and again, so an
# unbounded retry turns one broken start into a steady churn of processes. After
# each failure, every signal inside a backoff window is skipped: nothing is
# queued for later, and the first signal after the window starts normally. With
# a cap of 3 the reachable windows are 10s after the first failure and 20s after
# the second (the doubling and its 300s ceiling bound a larger cap). After the cap the slot stops starting in the background and says so once (one ERROR
# log line and one error row in the chat). From then on background starts stay
# OFF for that slot until a start succeeds -- the user's own next message still
# starts it, and that success clears the count -- or the gateway restarts: the
# count lives only on the in-memory slot, so a restart starts it again at zero.
#
# Only a failure of the agent START counts: an exception out of the session
# allocation in ``_spawn_admitted_prefetch``. A failed pending-reset consume, a
# binding/selection write, the admission bookkeeping around it, or a capability
# refusal raised before any process ran (``_is_pre_spawn_refusal``) spawned no
# agent process, so it is logged and does not move the slot toward the cap.
_EAGER_SPAWN_FAILURE_CAP = 3


_EAGER_SPAWN_BACKOFF_BASE_SECS = 10.0


_EAGER_SPAWN_BACKOFF_MAX_SECS = 300.0


#: Bound on the last-error text the stop notice carries into the chat.
_EAGER_SPAWN_ERROR_DETAIL_MAX_CHARS = 500


def _eager_spawn_backoff_secs(failures: int) -> float:
    """Seconds the next background start waits after *failures* in a row."""
    if failures <= 0:
        return 0.0
    return min(
        _EAGER_SPAWN_BACKOFF_BASE_SECS * (2 ** min(failures - 1, 16)),
        _EAGER_SPAWN_BACKOFF_MAX_SECS,
    )


def _eager_spawn_held_off(slot: "_ChatSlot") -> bool:
    """Whether *slot*'s failed starts hold its next background start back."""
    failures = getattr(slot, "_eager_spawn_failures", 0)
    if failures >= _EAGER_SPAWN_FAILURE_CAP:
        return True
    return time.monotonic() < getattr(slot, "_eager_spawn_retry_at", 0.0)


def _note_eager_spawn_failure(slot: "_ChatSlot", exc: BaseException) -> None:
    """Count a failed background start; at the cap, stop and tell the user."""
    failures = getattr(slot, "_eager_spawn_failures", 0) + 1
    slot._eager_spawn_failures = failures
    slot._eager_spawn_retry_at = time.monotonic() + _eager_spawn_backoff_secs(failures)
    if failures > _EAGER_SPAWN_FAILURE_CAP:
        # Already stopped and already said so. ``schedule_eager_spawn`` refuses a
        # slot at the cap, so this is only a spawn that was in flight when the
        # cap was reached; the stop is announced once, not per late failure.
        logger.debug("Eager spawn: slot %s failed again after the stop", slot.key)
        return
    if failures < _EAGER_SPAWN_FAILURE_CAP:
        logger.warning(
            "Eager spawn: slot %s failed to start (%d/%d); next background start " "in %.0fs",
            slot.key,
            failures,
            _EAGER_SPAWN_FAILURE_CAP,
            _eager_spawn_backoff_secs(failures),
        )
        return
    logger.error(
        "Eager spawn: slot %s failed to start %d times in a row; background starts "
        "stay off until a start succeeds or the gateway restarts",
        slot.key,
        failures,
    )
    # Redacted over the whole text, then bounded: an agent error can carry a
    # near-frame-limit payload, and this row is retained and broadcast.
    detail = redact_and_truncate(str(exc), _EAGER_SPAWN_ERROR_DETAIL_MAX_CHARS)
    try:
        slot.append(
            "error",
            f"This chat's agent failed to start {failures} times in a row, so it "
            "will not be started again in the background. Send a message to try "
            f"again. Last error: {detail or type(exc).__name__}",
            "msg msg-err",
        )
    except Exception:
        logger.debug("Eager spawn: could not post the stop notice for %s", slot.key, exc_info=True)


#: ``CapabilityStartupError`` codes raised only before any agent process exists:
#: the member, its enrollment state or its prepared generation cannot be read, or
#: the provider cannot host it. ``capability_runtime_unverified`` is raised only
#: after a process ran. ``capability_runtime_cwd_changed`` and
#: ``capability_startup_raced`` come from ``verify_saved``, which runs both before
#: the start and after it, so the code alone cannot say whether a process ran;
#: they are left counted, the side that keeps a churning start bounded.
_PRE_SPAWN_CAPABILITY_CODES = frozenset(
    {
        "capability_member_missing",
        "capability_state_unreadable",
        "capability_generation_missing",
        "capability_harness_unsupported",
        "capability_tool_limits_unsupported",
        "capability_runtime_not_fresh",
    }
)


def _is_pre_spawn_refusal(exc: BaseException) -> bool:
    """Whether *exc* refused a background start before any agent process ran.

    A ``CapabilityError`` is a refusal of the member's capability spec, which
    the user fixes in Capabilities rather than by retrying; counting it would
    end in a notice that blames a failed start. Pre-spawn capability startup
    codes are the same kind of refusal. Neither spawns a process, so neither
    feeds the churn the background-start cap bounds.
    """
    if isinstance(exc, CapabilityError):
        return True
    return isinstance(exc, CapabilityStartupError) and str(exc) in _PRE_SPAWN_CAPABILITY_CODES


def _clear_eager_spawn_failures(slot: "_ChatSlot") -> None:
    """Forget *slot*'s failed starts once one start succeeded."""
    slot._eager_spawn_failures = 0
    slot._eager_spawn_retry_at = 0.0


# How long a speculatively RESUMED session may sit unclaimed before it is
# torn down. A resumed session holds kiro-cli's native per-session lock, so
# a prefetch the user walked away from must release it cleanly rather than
# wait out the 30-minute idle sweep. Fresh (non-resumed) eager sessions hold
# no prior transcript's lock, so they skip the TTL and are reaped by the idle
# sweep — but they still count against the live-population cap below.
_RESUME_PREFETCH_TTL_SECS = 600.0


# Population cap on live-but-unclaimed speculative sessions — resumed AND
# fresh. The spawn semaphore bounds concurrent SPAWNS, not accumulated LIVE
# processes: restored resumable tabs flipped through after a gateway restart,
# or slots created/reconfigured in sequence, each stack one full kiro-cli
# process (RSS, plus the session's own MCP servers; the native session lock
# too when resumed) until the TTL or idle sweep fires. The cap is HOST-
# DERIVED (``resource_status.prewarm_allowance``): the fixed ceiling below
# on an ample host, one session when memory is tight, none when it is
# critical — so a host that cannot afford idle agent processes stops
# pre-warming them instead of stacking one per slot and then evicting. The
# allowance is applied twice: as ADMISSION before the spawn (room is made by
# evicting the oldest unclaimed session first, or the spawn is skipped when
# the allowance is zero or making room fails) and, PROBED AFRESH, as
# EVICTION after registration, for an allowance that shrank while the
# handshake ran. A claimed session is never touched by either — it just
# falls out of the accounting.
_RESUME_PREFETCH_MAX_LIVE = resource_status.PREWARM_MAX_LIVE


# Registry value for a key whose spawn is admitted but not yet registered. It
# counts against the allowance like a live entry -- the process is about to
# exist -- but there is nothing to evict yet, so eviction skips it. Two slot
# signals admitted concurrently against an allowance of one would otherwise
# both pass (each seeing an empty registry) and both spawn.
_RESERVED = object()


def _entry_arm_generation(value: object) -> float:
    """The arm generation a registry entry registered under.

    Entries predating the generation scheme (or seeded by tests) carry
    ``None``; treat them as generation zero -- older than every signal, so
    always within eviction reach.
    """
    return value if isinstance(value, (int, float)) else 0


def _prewarm_allowance() -> int:
    """Host-derived cap on idle pre-warmed sessions (see ``resource_status``).

    Reads ``/proc/meminfo`` (or the platform equivalent), so callers on the
    loop run it via ``asyncio.to_thread``. One seam for tests to pin a host
    shape without making the population tests host-dependent.
    """
    return resource_status.prewarm_allowance()


def _pressure_hold_blocks_prewarm(state: "DashboardState") -> bool:
    """Whether the subagent gate's macOS kernel memory-pressure hold applies now.

    Read at a pre-warm's ADMISSION only, never on the re-probe after one
    registered: a speculative runtime must not take the memory held user starts
    wait for, but one that already finished its handshake is not evicted for a
    level that crossed WARN meanwhile. On the loop: the manager's state is
    loop-owned, and the read is a sysctl (plus a cached config read only while
    the level is held).
    """
    subagents = getattr(state, "subagents", None)
    if subagents is None:
        return False
    try:
        return subagents.memory_pressure_hold_active() is True
    except Exception:
        logger.debug("Eager spawn: pressure-hold read failed", exc_info=True)
        return False


async def _evict_prefetches_beyond(
    sessions: Any,
    limit: int,
    *,
    keep: str | None = None,
    up_to_generation: float | None = None,
) -> bool:
    """Evict oldest unclaimed prefetches until at most *limit* remain.

    *keep* is a key exempt from eviction AND from the count — the session being
    (re-)armed, whose own registration is accounted for by the caller.

    *up_to_generation* bounds which entries are within reach: only entries
    registered at or before that arm generation may be evicted. ``None``
    means unbounded — the post-registration cap and the critical band evict
    regardless of age. Admission passes its signal's generation so it never
    tears down a session a CONCURRENT signal registered while this one was
    still working toward its admission (see ``_arm_generation``).

    Returns ``True`` once the population is within *limit*. A failed removal
    returns ``False`` at once and LEAVES the entry registered: the process is
    still live, so it still counts, and the caller must not treat the room as
    made. The entry is retried by the next eviction and drops out lazily when
    that attempt finds the session already claimed or gone.
    """
    while len([k for k in _armed_prefetches if k != keep]) > limit:
        evictable = [
            k
            for k, v in _armed_prefetches.items()
            if k != keep
            and v is not _RESERVED
            and (up_to_generation is None or _entry_arm_generation(v) <= up_to_generation)
        ]
        if not evictable:
            # Every entry over the limit is out of reach: a reservation (an
            # admitted spawn whose process does not exist yet) or a session a
            # newer-generation signal registered. Room cannot be made; the
            # caller treats this like a failed eviction — the reservation's
            # owner registers or rolls back, and a concurrent registrant
            # keeps the allowance it already spent.
            return False
        oldest = evictable[0]
        # The removal below awaits; a focus/reload signal can re-register the
        # SAME key with a newer generation meanwhile. Capture the entry's
        # value and pop only if it is still the one this eviction selected —
        # unconditionally popping by key would erase the replacement's
        # accounting and let the next admission spawn on top of it.
        oldest_value = _armed_prefetches.get(oldest)
        try:
            # Shielded for the same reason as the TTL removal: an interrupted
            # removal leaks the process holding the native lock.
            if await asyncio.shield(sessions.remove_if_unclaimed(oldest)):
                logger.info(
                    "Resume prefetch: evicted oldest unclaimed %s (cap %d)",
                    oldest,
                    limit,
                )
            # False = claimed or already gone — either way it does not count
            # against the cap; dropping the registry entry suffices.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Resume prefetch: eviction failed for %s", oldest, exc_info=True)
            return False
        if _armed_prefetches.get(oldest) is oldest_value:
            _armed_prefetches.pop(oldest, None)
        # A changed value means a re-registration won the window: leave its
        # entry alone and let the loop re-evaluate — under an admission's
        # generation bound the fresh entry is out of reach, so the admission
        # refuses rather than tearing it down.
    return True


async def _admit_prefetch(
    sessions: Any, new_key: str, allowance: int, *, signal_generation: float | None = None
) -> bool:
    """Make room for *new_key* BEFORE it spawns; ``False`` when it must not.

    A zero allowance refuses, and first evicts every idle pre-warm already
    live: the host is in the critical band, so the idle population must fall
    to zero, not merely stop growing. Otherwise the oldest
    unclaimed sessions are evicted until *new_key* fits, so the live
    population never exceeds the allowance even transiently — spawning first
    and evicting after would overshoot by one full process for the length of
    the handshake. An eviction that FAILS refuses too: the room was not made,
    so admitting on top of it would exceed the allowance just the same.

    *signal_generation* is the arm generation current when this admission's
    slot signal arrived (``None``: read it now). Eviction is bounded by it,
    so an admission delayed past a concurrent signal's registration — the
    allowance probe runs in a worker thread, and Windows scheduling can hold
    it beyond the other signal's whole handshake — refuses instead of tearing
    down the session that signal spawned moments ago and spawning a second
    one. A genuinely LATER signal reads a generation at or
    above every live registration, so newest-signal-wins eviction is intact.

    Admission RESERVES *new_key* in the registry before returning ``True``: the
    eviction above awaits, and two slot signals admitted in the same window
    would otherwise each see the room the other is about to fill. The
    reservation counts against every later admission's allowance and is
    converted by ``_cap_armed_prefetches`` at registration; the caller rolls
    it back (``_release_prefetch_reservation``) on every path that ends
    without one. A key already registered is re-armed in place and is not
    reserved again, so a rollback cannot drop a live entry.
    """
    if signal_generation is None:
        signal_generation = _arm_generation
    if allowance <= 0:
        # The band that refuses a new pre-warm also has no room for the ones
        # already idle, *new_key*'s own earlier pre-warm included -- a re-arm
        # in this band gets no exemption, and neither does a concurrent
        # signal's fresh registration: the host is critical, so the idle
        # population falls to zero regardless of age. A failed eviction is
        # logged by the helper and the next signal retries it.
        await _evict_prefetches_beyond(sessions, 0)
        return False
    if not await _evict_prefetches_beyond(
        sessions, allowance - 1, keep=new_key, up_to_generation=signal_generation
    ):
        return False
    if new_key not in _armed_prefetches:
        _armed_prefetches[new_key] = _RESERVED
    return True


def _release_prefetch_reservation(new_key: str) -> None:
    """Drop *new_key*'s admission reservation if registration never converted it."""
    if _armed_prefetches.get(new_key) is _RESERVED:
        del _armed_prefetches[new_key]


async def _cap_armed_prefetches(
    sessions: Any, new_key: str, cap: int = _RESUME_PREFETCH_MAX_LIVE
) -> None:
    """Register *new_key* as armed and evict oldest unclaimed beyond *cap*.

    The just-registered key is the newest and is never the one evicted, so the
    bound applies to the OTHER entries: at most ``cap - 1`` of them stay. A
    failed eviction here is logged and left registered for the next attempt;
    the new session is already live, so there is no spawn left to refuse.

    A *cap* of zero is the critical band (see ``_prewarm_allowance``): the host
    tightened while the handshake ran and no unclaimed pre-warm may stay, the
    one just spawned included. Every entry is evicted, *new_key* too; the
    first real turn on that slot cold-starts, exactly as if the allowance had
    read zero before the spawn and ``_admit_prefetch`` had refused.
    """
    global _arm_generation
    _arm_generation += 1
    _armed_prefetches.pop(new_key, None)  # re-arm moves the key to newest
    _armed_prefetches[new_key] = _arm_generation
    if cap <= 0:
        await _evict_prefetches_beyond(sessions, 0)
        return
    await _evict_prefetches_beyond(sessions, cap - 1, keep=new_key)


def schedule_eager_spawn(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    allow_resume: bool = False,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> "asyncio.Task | None":
    """Speculatively create *slot*'s session ahead of its first message.

    Fire-and-forget: called from the slot-create and project-set handlers so
    the multi-second ACP handshake (spawn + session/new, or session/load for
    a resumable slot) overlaps with the user's think-time instead of being
    paid on the first send. No-op unless ``session.eager_spawn`` is enabled.

    At most one pending task per slot: a newer signal cancels the older task,
    so the spawn always reflects the slot's settled agent/model/project.

    ``allow_resume`` opts this spawn into resume prefetch (the slot-focused
    intent signal): a resumable key performs the speculative ``session/load``
    instead of skipping, with the ``resumed=True`` observation armed for the
    first real turn and a TTL teardown if no turn ever claims it. The other
    intent signals keep the refusal — slot create has no mapping, and the
    agent/project switch handlers reset the session themselves.

    ``start_priority`` is FOREGROUND only from a handler serving the dashboard
    owner's own slot action (create, agent or project switch), where the person is
    about to type; the focus-driven resume prefetch, reloads and app callers stay
    BACKGROUND (rule: ``kiro_crew.start_priority``).

    The flag is read from the live-config watcher's adopted snapshot, a plain
    attribute read, and there is deliberately no disk fallback behind it: this
    function runs ON the event loop, and the watcher is primed only after the
    listener binds, so a load here would stall the loop for exactly the requests
    that arrive in that window (no-blocking-call-on-event-loop). An unprimed
    watcher therefore skips the spawn, which costs a cold start on the slot's
    first turn and nothing else. An armed snapshot is trusted whatever its
    ``degraded_sections`` reports: the marker is sticky for the life of the
    process, and while a document does not parse the snapshot is the copy
    holding the operator's real setting.
    """
    try:
        from kiro_crew.config import live

        cfg = live.snapshot()
        if cfg is None or not cfg.session.eager_spawn:
            return None
        # Shared Crew: with fair admission on, a runtime starts only inside an
        # admitted turn, so creating sessions cannot start unbounded processes.
        from kiro_crew.shared_crew.turn_gate import admission_active

        if admission_active():
            return None
    except Exception:
        return None
    if _eager_spawn_held_off(slot):
        logger.debug("Eager spawn: slot %s is backing off after failed starts", slot.key)
        return None
    if app_agent_binding_applies(slot):
        # An app-owned palette session resolves its agent only after the per-turn
        # ownership check; a speculative start would resolve it before that check.
        return None
    prev = getattr(slot, "_eager_spawn_task", None)
    if prev is not None and not prev.done():
        prev.cancel()
    # The arm generation as of THIS slot signal, read synchronously HERE:
    # create_task only queues the coroutine, so a registration landing before
    # its first step would otherwise be inside a later snapshot and look
    # evictable to the very signal it raced.
    task = asyncio.create_task(
        _eager_spawn(
            state,
            slot,
            allow_resume=allow_resume,
            signal_generation=_arm_generation,
            start_priority=start_priority,
        )
    )
    slot._eager_spawn_task = task
    return task


def _slot_binding(slot: "_ChatSlot") -> tuple[str, str, str, str, str, str]:
    """The slot bindings an eager handshake bakes into the session it registers.

    ONE definition, because two exist to be compared: ``_eager_spawn`` snapshots
    this before the handshake and ``_spawn_admitted_prefetch`` re-reads it after,
    tearing the session down if they differ. A second, hand-written tuple on
    either side is not a copy of this contract, it is a silent inversion of it —
    a field present here and missing there makes the comparison unequal on every
    call, so the guard removes the session it is supposed to keep.
    """
    return (
        slot.agent,
        slot.model,
        slot.project,
        slot.reasoning_effort,
        slot.memory_store,
        # The namespace the name is read in: a kind change is a binding change.
        slot.agent_kind,
    )


async def _runs_on_a_session_environment(session_key: str) -> bool:
    """Whether ``session_key`` is bound to a remote Session Environment profile.

    Read from the persisted choice, never from what one request just did, so no
    caller can prewarm a bound chat. An unreadable choices store answers yes: a
    skipped prewarm costs one cold start, a wrong one starts billed compute.
    Always no while the developer flag is off (the choice is never read).
    """
    from kiro_crew.session_environment import (
        SessionEnvironmentUnavailable,
        _choices,
        session_environments_enabled,
    )

    if not session_environments_enabled():
        return False
    try:
        return await asyncio.to_thread(_choices().get, session_key) is not None
    except (OSError, ValueError, SessionEnvironmentUnavailable):
        return True


async def _eager_spawn(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    allow_resume: bool = False,
    signal_generation: float | None = None,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> None:
    """Debounce, re-validate, then create the slot's session and release it.

    Ordering is load-bearing:

    1. The turn-in-flight bail (``slot.running``) MUST precede the pending-
       reset consume. The project-set endpoint is reachable from inside the
       kiro-cli process group via the ``set_project`` MCP tool, and consuming
       the reset kills that session's process group — mid-turn that would
       kill the caller, which is exactly what the deferred-reset design
       exists to prevent. When a turn is running, its own end-of-turn path
       consumes the reset instead.
    2. Global and named V1 sessions may reach ``get_or_create``, which acquires
       the per-session semaphore and is released immediately below because no
       turn follows. Private V2 sessions stop before allocation so the first
       real turn can validate and pin their trusted store before a provider is
       registered. For eligible V1 sessions, a first message arriving during
       the handshake blocks on the same semaphore and reuses the created
       session.
    """
    try:
        # The arm generation as of this slot signal. ``schedule_eager_spawn``
        # reads it synchronously at signal arrival (create_task only queues
        # this coroutine, so reading it here would already be one scheduling
        # hop late); a direct call falls back to reading it now, before any
        # await. Every registration that lands after the snapshot belongs to
        # a concurrent signal, and the admission below must not evict it.
        if signal_generation is None:
            signal_generation = _arm_generation
        await asyncio.sleep(_EAGER_SPAWN_DEBOUNCE_SECS)
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return
        if state.get_slot(slot.key) is not slot:
            return  # slot deleted or replaced while debouncing
        if slot.running:
            return  # a real turn owns session creation (and the pending reset)
        if app_agent_binding_applies(slot):
            return  # bound after the debounce: the per-turn check owns resolution
        if _eager_spawn_sem.locked():
            logger.info("Eager spawn: concurrency cap reached, skipping slot %s", slot.key)
            return
        async with _eager_spawn_sem:
            await _consume_pending_reset(state, slot)
            if slot._pending_reset_history_key:
                # The deferred reset could not land (busy channel turn,
                # attached children, cold-starting session): pre-warming now
                # would REUSE or register a session under a key whose
                # teardown is still owed, and the first real turn would run
                # on the stale bindings the armed flag exists to remove. The
                # consumer's retry task owns the follow-up; eager spawn
                # simply stands down.
                return
            session_key = effective_session_key(slot)
            if await _runs_on_a_session_environment(session_key):
                # Its start creates or wakes a billed remote workspace: that is
                # left to the first real message, whichever signal armed this.
                return
            if allow_resume:
                # The focus signal only ever adds the RESUME case; fresh eager
                # spawn stays owned by the create/project/agent signals. This
                # probe is the in-memory hint (no disk, no pruning): SessionMap
                # is loop-owned and unlocked, so the pruning ``resumable_sid``
                # lookup must not run in a worker thread — a prune there would
                # race concurrent loop-side map writes. The authoritative
                # pruning lookup happens inside get_or_create's resume path,
                # on the loop, where it always ran; a false-positive hint just
                # means the speculative load falls back and is torn down below.
                if not sessions.resumable_hint(session_key):
                    return

            # Snapshot the bindings the handshake is about to bake in. A
            # switch handler (workspace, model, reasoning effort) that fires
            # mid-handshake resets the session key — but the reset no-ops
            # because nothing is registered yet, so without this check the
            # eager task would register a session carrying the OLD bindings
            # and the first real turn would silently reuse it (e.g. run tools
            # in the wrong workspace). Agent/project changes re-arm through
            # schedule_eager_spawn and cancel this task, but the other
            # switches don't — the snapshot covers them all uniformly.
            _bound = _slot_binding(slot)
            kiro_agent: str | None = None
            # Canonical crew identity for watchdog overrides. Seeded from the
            # slot, replaced by the resolver's alias below. An EMPTY slot runs
            # the default TEMPLATE (resolve_agent_bindings step 2), which is
            # nobody's crew, so "" is its honest alias.
            crew_alias = slot.agent or ""
            agent_model = ""
            resolved_ok = False
            try:
                # Migration writes apply a delta to a fresh document, preserving other settings.
                cfg = await asyncio.to_thread(KiroCrewConfig.load)
                bindings = await asyncio.to_thread(
                    functools.partial(resolve_session_agent_bindings, agent_kind=_bound[5]),
                    resolve_agent_bindings,
                    cfg,
                    session_key,
                    _bound[0] or None,
                )
                kiro_agent = bindings.kiro_agent
                crew_alias = bindings.resolved_alias
                agent_model = normalize_agent_model(bindings.model)
                # SELF-HEAL mirror of the real turn's guard: an app-owned slot
                # whose agent read cold from the materialized snapshot would bake
                # the DEFAULT agent into this speculative session, forcing the
                # first real turn to discard and cold-start it. Recover in two
                # escalating steps — (1) RESCAN the snapshot off the loop (never
                # raises) in case the spec is on disk but cold, re-resolve once;
                # (2) if still unresolved, RE-REGISTER this app's agents FROM
                # SOURCE off the loop (covers "spec never materialized though
                # source intact") and re-resolve again — so the pre-warmed
                # session carries the app's own agent. No fail-loud here — the
                # eager path is best-effort and tears itself down on any miss;
                # the real turn owns the user-facing failure.
                if slot._app and not bindings.requested_resolved:
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            subprocess_executor(), refresh_materialized_agents
                        )
                    except Exception:  # noqa: BLE001 — warm failure only costs a re-resolve miss
                        logger.warning(
                            "Eager spawn: failed to warm materialized agents for slot %s",
                            slot.key,
                            exc_info=True,
                        )
                    bindings = await asyncio.to_thread(
                        functools.partial(resolve_session_agent_bindings, agent_kind=_bound[5]),
                        resolve_agent_bindings,
                        cfg,
                        session_key,
                        _bound[0] or None,
                    )
                    kiro_agent = bindings.kiro_agent
                    crew_alias = bindings.resolved_alias
                    agent_model = normalize_agent_model(bindings.model)
                    if not bindings.requested_resolved:
                        bindings = await _recover_app_agent_binding(cfg, slot, project=None)
                        kiro_agent = bindings.kiro_agent
                        crew_alias = bindings.resolved_alias
                        agent_model = normalize_agent_model(bindings.model)
                if bindings.selection_kind == "template":
                    crew_alias = ""
                resolved_ok = bindings.requested_resolved
                await asyncio.to_thread(
                    _require_session_memory_assignment, session_key, bindings.memory_store_name
                )
            except Exception:
                logger.warning(
                    "Eager spawn: failed to resolve agent bindings for slot %s",
                    slot.key,
                    exc_info=True,
                )
                return
            # Fail-safe mirror of the real turn's guard: if an app-owned slot
            # STILL did not resolve (self-heal missed, or the resolve threw), do
            # NOT register a speculative session — resolve_agent_bindings returns
            # the DEFAULT agent on a cold miss, so registering here would bind the
            # wrong agent and the first real turn could reuse that session instead
            # of hitting its own _AppAgentNotLoaded guard. Bail; the first real
            # turn self-heals and, if still cold, fails loud.
            if slot._app and not resolved_ok:
                logger.info(
                    "Eager spawn: app slot %s unresolved after warm; leaving to first turn",
                    slot.key,
                )
                return
            if slot.agent and not resolved_ok:
                # The real turn refuses an unavailable explicit Crew Member.
                # Do not pre-register the resolver's Global fallback first;
                # doing so would leave an unrelated provider behind the error.
                logger.info(
                    "Eager spawn: Crew Member %r is unresolved; leaving to first turn",
                    slot.agent,
                )
                return
            # A member's native launch documents must be captured from its
            # admitted identity before startup. Leave member sessions to the
            # first real turn instead of borrowing an ordinary eager provider.
            #
            # A restored slot's recorded store is independently authoritative.
            # If it disagrees with today's resolver (deleted/renamed member,
            # stale alias, or an in-flight switch), stand down rather than
            # warming a provider with either identity. The real turn owns the
            # fail-loud explanation. Valid named V1 stores keep the legacy eager
            # path; classification uses the config and captured execution.
            from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

            resolved_store = bindings.memory_store_name
            restored_store = slot.memory_store
            if not isinstance(resolved_store, str):
                logger.info(
                    "Eager spawn: slot %s has no resolved memory store; leaving to first turn",
                    slot.key,
                )
                return
            if restored_store and (
                not isinstance(restored_store, str) or restored_store != resolved_store
            ):
                logger.info(
                    "Eager spawn: slot %s restored memory binding disagrees with resolver; "
                    "leaving to first turn",
                    slot.key,
                )
                return
            selected_store = restored_store or resolved_store or DEFAULT_MEMORY_STORE
            if selected_store != DEFAULT_MEMORY_STORE:
                record = cfg.memory_stores.get(selected_store)
                version = getattr(record, "memory_version", None)
                owner = getattr(record, "owner_member", None)
                if version == 2 and isinstance(owner, str) and owner:
                    logger.info("Eager spawn: member context slot %s left to first turn", slot.key)
                    return
                if version != 1 or owner != "":
                    logger.info(
                        "Eager spawn: slot %s has an unavailable or inconsistent memory "
                        "binding; leaving to first turn",
                        slot.key,
                    )
                    return
            # A resume the provider answers with a fresh session plus replay
            # (Tool Search on, kiro backend, direct dashboard key) can only
            # come back ``resumed=False`` and be refused -- after a whole
            # runtime was spawned and torn down. Ask the provider's own
            # decision before anything exists; the first real turn takes the
            # same fresh-session path it always does. The backend is the
            # factory's own selection for this key, and the prefetch allocates
            # with no channel identity.
            if allow_resume and resume_takes_tool_search_replay(
                tool_search=cfg.agent.tool_search,
                backend=select_provider_backend(
                    session_key, cfg.agent.member_acp_backend, cfg.agent.acp_backend
                ),
                channel_id=None,
                session_key=session_key,
            ):
                logger.info("Eager spawn: %s left to first turn (tool-search replay)", session_key)
                return
            # Agent recovery awaits. A real
            # turn or a slot mutation may therefore have landed after the
            # initial gate. Nothing is registered yet, so simply stand down;
            # the current owner will allocate from its current bindings.
            if (
                state.get_slot(slot.key) is not slot
                or slot.running
                or _slot_binding(slot) != _bound
            ):
                return
            await asyncio.to_thread(
                record_agent_selection,
                session_key,
                _bound[0],
                bindings,
                memory_mode=slot.memory_mode,
            )
            if (
                state.get_slot(slot.key) is not slot
                or slot.running
                or _slot_binding(slot) != _bound
            ):
                return
            if shutdown_event.is_set():
                # The gateway began stopping during the awaits above. Its
                # session teardown cannot see a process that is not registered
                # yet, so a start beginning now would outlive it.
                return
            # ADMISSION, before the process exists: the live-population cap is
            # host-derived, and on a host that cannot afford another idle
            # agent process the right move is to not spawn it — the first
            # message cold-starts exactly as if eager spawn never ran. Room
            # is made first (oldest unclaimed evicted) so the population never
            # overshoots during the handshake. Off the loop: it reads procfs.
            allowance = await asyncio.to_thread(_prewarm_allowance)
            if allowance > 0 and _pressure_hold_blocks_prewarm(state):
                # No new speculative runtime, but the idle ones already live are
                # not evicted: they hold no more memory than they did, and the
                # host bands below, not the hold, decide when those must go.
                logger.info(
                    "Eager spawn: the subagent memory-pressure hold applies; leaving "
                    "slot %s to first turn",
                    slot.key,
                )
                return
            if not await _admit_prefetch(
                sessions, session_key, allowance, signal_generation=signal_generation
            ):
                logger.info(
                    "Eager spawn: host memory admits %d pre-warmed session(s) and "
                    "%d are live; leaving slot %s to first turn",
                    allowance,
                    len(_armed_prefetches),
                    slot.key,
                )
                return
            _t0 = time.monotonic()
            try:
                await _spawn_admitted_prefetch(
                    state,
                    slot,
                    sessions,
                    session_key,
                    _t0,
                    kiro_agent=kiro_agent,
                    crew_alias=crew_alias,
                    agent_model=agent_model,
                    allow_resume=allow_resume,
                    _bound=_bound,
                    start_priority=start_priority,
                )
            finally:
                # Every exit that is not a registration -- refused, another
                # creator won, slot vanished, bindings changed, spawn raised,
                # cancelled -- gives the reserved allowance back.
                _release_prefetch_reservation(session_key)
    except asyncio.CancelledError:
        raise
    except SessionClosingError:
        # Same shutdown race as _on_session_closing: the gateway began
        # closing while the speculative spawn was mid-start. Not a failure —
        # the allocation path already reaped the half-started provider.
        logger.info("Eager spawn for %s aborted — gateway is shutting down", slot.key)
    except Exception:
        # Logged, not counted: the agent-start failures that count toward the
        # background-start cap were already noted at the allocation itself
        # (``_spawn_admitted_prefetch``); everything else reaching here -- a
        # pending-reset consume, a selection write, admission bookkeeping --
        # spawned no agent and must not stop background starts.
        logger.warning("Eager spawn failed for slot %s", slot.key, exc_info=True)


async def _spawn_admitted_prefetch(
    state: "DashboardState",
    slot: "_ChatSlot",
    sessions: Any,
    session_key: str,
    _t0: float,
    *,
    kiro_agent: str | None,
    crew_alias: str,
    agent_model: str,
    allow_resume: bool,
    _bound: tuple,
    start_priority: StartPriority,
) -> None:
    """The admitted half of ``_eager_spawn``: handshake, guards, registration.

    Split out so the caller can bracket it in one ``try/finally`` that releases
    the admission reservation; a ``return`` from any guard below lands there.
    """
    try:
        # The store this slot was writing, read before the allocation below maps
        # its own session over it. This is the EARLIER of the two allocation
        # sites, so it is the only one that can still see the predecessor: once
        # this registers, the slot's mapping names the successor, and the first
        # real turn reading the mapping itself would read that successor and
        # write no `previous` edge at all -- leaving the superseded store
        # unlinked, so nothing joins the slot's history across the restart, which is
        # the failure this edge exists to remove. The latch is write-once, so a turn that
        # observes afterwards cannot replace this with the successor's id.
        #
        # The fallback pair the latch uses when this slot has no record of its own:
        # the units this slot's key names, ordered by the succession edges they
        # recorded, with the slot-to-session mapping behind them. Durable, so a
        # gateway that restarts inside a replay-pending window reads the store the
        # slot was really on instead of the mapping's generation-older answer.
        _previous = await _slot_predecessor_store(sessions, slot, session_key)
        slot.latch_crew_log_previous(
            _previous.sid,
            undecided=_previous.undecided,
            from_mapping=_previous.from_mapping,
        )
        # speculative=True keeps the one-shot first-turn flag armed for
        # the real first message (atomically, at registration) and
        # refuses resumable keys — unless allow_resume opted in, in
        # which case the speculative session/load runs here and the
        # resumed=True observation is armed for the real turn. See
        # get_or_create's docstring.
        # A slot or crew pin is the caller's own selection; with neither, the
        # allocation resolves the default-model ladder itself, exactly as it
        # will for the first real turn.
        _requested_model = slot.model or agent_model or ""
        try:
            _, is_new, resumed = await sessions.get_or_create(
                session_key,
                agent=kiro_agent or slot.agent or None,
                # Canonical crew identity — the resolver's alias, which
                # covers the default crew on an empty slot; plumbed to the
                # session so per-agent watchdog windows never depend on a
                # cross-namespace name match. "" is authoritative: no
                # alias applied, so no override applies.
                crew_agent=crew_alias,
                model=_requested_model or None,
                cwd=slot.project or None,
                speculative=True,
                speculative_resume=allow_resume,
                reasoning_effort_override=slot.reasoning_effort or None,
                start_priority=start_priority,
                **_app_allocation_kwargs(slot),
            )
        except (SpeculativeResumeRefused, SessionClosingError, SessionEndingError):
            # A refusal, a gateway shutdown, or a key being ended: no agent
            # start was attempted and failed, so none of them is counted.
            raise
        except Exception as exc:
            # The one place a background start can fail to START the agent
            # (a failed spawn or initialize handshake). Counted here, not in
            # ``_eager_spawn``'s catch-all, so a reset or binding error raised
            # around this call never moves the slot toward the cap. A
            # capability refusal raised before any process ran is not one.
            if not _is_pre_spawn_refusal(exc):
                _note_eager_spawn_failure(slot, exc)
            raise
    except SpeculativeResumeRefused:
        # Two sources: the entry gate (resumable key, resume not
        # opted in — fresh eager spawn leaves it to the first turn)
        # or a failed speculative LOAD (allow_resume path: F2 fell
        # back / mapping vanished / provider switch), rejected before
        # registration so no claimable fallback session exists. Both
        # end the same way: the first real message handles it.
        logger.info("Eager spawn: %s left to first turn (refused)", session_key)
        return
    sessions.release(session_key)
    # The cleanup below may only tear down a session THIS task created.
    # is_new=False means another creator won the same-key race (or the
    # claim attached to an already-registered session): a real turn
    # owns that runtime, may have finished its turn already, and may
    # have background work (subagents) still attached — removing it
    # here would terminate the winner's session out from under it. The
    # winner registered with its own current bindings, so the stale-
    # bindings hazard these guards exist for does not apply to it.
    if not is_new:
        logger.info("Eager spawn: another creator won %s, leaving session alone", session_key)
        return
    # The slot can be deleted while the handshake ran; the delete
    # handler's sessions.remove() may have executed before this task
    # registered the session, which would leave an orphan that a
    # recreated slot with the same key would silently reuse with THIS
    # slot's (now stale) agent/cwd bindings. Tear it down.
    if state.get_slot(slot.key) is not slot:
        logger.info("Eager spawn: slot %s vanished mid-handshake, removing session", slot.key)
        await sessions.remove(session_key)
        return
    # Same shape for a binding change: a switch handler's reset ran
    # before registration and found nothing, so the session we just
    # registered carries stale bindings. Remove it — the first real
    # message cold-starts with the current bindings, exactly as if
    # eager spawn never ran.
    if _slot_binding(slot) != _bound:
        logger.info(
            "Eager spawn: slot %s bindings changed mid-handshake, removing session",
            slot.key,
        )
        await sessions.remove(session_key)
        return
    if not resumed:
        # Stamp first, matching the turn site. This call IS the allocation, so the
        # two agree whenever a tier resolved one, and the stamp additionally carries
        # the id `get_or_create` resolved for itself when every tier deferred. One
        # order at both sites, because two orders on one field is what invites a
        # reader to think they name different things.
        slot._session_requested_model = (
            sessions.allocation_requested_model(session_key) or _requested_model
        )
    _clear_eager_spawn_failures(slot)
    logger.info(
        "Eager spawn: session ready for %s in %.0fms (new=%s resumed=%s)",
        session_key,
        (time.monotonic() - _t0) * 1000.0,
        is_new,
        resumed,
    )
    if allow_resume and resumed:
        _schedule_prefetch_ttl(state, slot, session_key)
    # Fresh and resumed sessions alike count against the live-
    # population cap: without this, sequential slot signals (create,
    # agent/project set) stack one unclaimed agent process per slot
    # until the idle sweep — the semaphore above only bounds
    # concurrent handshakes. The TTL stays resume-only; fresh
    # sessions hold no native session lock. The allowance is probed
    # AGAIN here rather than reusing the admission value: the host
    # may have tightened while the handshake ran, and a cap read
    # before the spawn cannot see that.
    allowance = await asyncio.to_thread(_prewarm_allowance)
    await _cap_armed_prefetches(sessions, session_key, cap=allowance)
    # allow_resume and not resumed cannot happen: a speculative
    # resume whose load fell back is rejected BEFORE registration
    # (SpeculativeResumeRefused, caught above) precisely so no
    # claimable fallback session ever exists — a real turn queued
    # during the load would otherwise claim it and strand its
    # exchanges behind the preserved old sid.


def _schedule_prefetch_ttl(state: "DashboardState", slot: "_ChatSlot", session_key: str) -> None:
    """Arm the unclaimed-prefetch teardown for a speculatively RESUMED session.

    One pending TTL per slot: a newer prefetch cancels the older timer, so the
    countdown always covers the most recent load. The teardown itself is
    conditional — ``remove_if_unclaimed`` no-ops once a real turn consumed the
    one-shot markers or a claimant holds the semaphore — so a TTL that fires
    after the user came back does nothing.
    """
    prev = getattr(slot, "_prefetch_ttl_task", None)
    if prev is not None and not prev.done():
        prev.cancel()
    slot._prefetch_ttl_task = asyncio.create_task(_prefetch_ttl(state, slot, session_key))


async def _prefetch_ttl(state: "DashboardState", slot: "_ChatSlot", session_key: str) -> None:
    """Tear down a resume-prefetched session no real turn claimed in time.

    A resumed session pins kiro-cli's native per-session lock; leaving an
    abandoned prefetch to the 30-minute idle sweep holds that lock (and the
    process's RSS) far longer than the speculation was worth. The removal
    preserves the session map, so the next focus or first message resumes
    again normally.
    """
    try:
        await asyncio.sleep(_RESUME_PREFETCH_TTL_SECS)
        _armed_prefetches.pop(session_key, None)  # arm window over either way
        sessions = getattr(state, "sessions", None)
        if sessions is None:
            return
        _current = state.get_slot(slot.key)
        if _current is not None and _current is not slot:
            return  # slot replaced — the new occupant owns this key now
        if _current is None:
            # Slot DELETED — do not assume the delete handler cleaned up this
            # session: it removes the slot-key-derived history session, while
            # a channel-born slot's prefetch registered under its LINKED
            # session key (effective_session_key). Returning here would leak
            # that process holding kiro-cli's native lock. Fall through to the
            # conditional removal — it no-ops on an already-removed key and
            # never touches a claimed session (e.g. the channel side using
            # the linked session).
            pass
        elif slot.running:
            return  # a real turn claimed (or is claiming) the session
        # Shielded: a cancel landing after remove_if_unclaimed has popped the
        # registry entry but before provider.shutdown() finishes would leak
        # the process holding kiro-cli's native lock — nothing else can find
        # it anymore. The shield lets the removal run to completion while the
        # cancel still propagates to this task.
        if await asyncio.shield(sessions.remove_if_unclaimed(session_key)):
            logger.info(
                "Resume prefetch: unclaimed session %s expired after %.0fs",
                session_key,
                _RESUME_PREFETCH_TTL_SECS,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Prefetch TTL failed for slot %s", slot.key, exc_info=True)


def stop_eager_spawns(state: "DashboardState") -> "list[asyncio.Task[None]]":
    """Cancel every slot's pending speculative start, for a gateway shutdown.

    A start still in its debounce would otherwise wake during the shutdown and
    spawn an agent process the shutdown does not own: nothing is registered
    yet, so ``close_all`` cannot reach it. Returns the cancelled tasks so the
    caller can await them; one cancelled mid-spawn finishes killing its
    process before it ends.
    """
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict):
        return []
    stopped: list[asyncio.Task[None]] = []
    for slot in list(slots.values()):
        task = getattr(slot, "_eager_spawn_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            stopped.append(task)
    return stopped
