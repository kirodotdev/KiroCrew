"""Per-session idle timers for ``session.idle_compaction``.

A dashboard chat that goes quiet past its backend's prompt-cache lifetime pays to
re-send its whole history at the uncached rate on the next turn. With the setting
on, the turn that ends a chat's activity arms one timer here. It is due a little
before that lifetime runs out (:func:`kiro_crew.agent_sdk.backends.idle_compaction_delay_secs`),
counted from a moment no later than the start of the turn's last model request
(:func:`provider_request_floor`), because that start is where the cache lifetime
begins.
When it fires, :meth:`IdleCompaction.compact_if_idle` decides whether the chat is
still idle and, if so, hands it to the coordinator's own ``_compact_session`` with
the idle trigger, so the compaction itself is the one the threshold path runs.

:class:`IdleCompaction` owns this trigger for one
:class:`kiro_crew.session_compaction.CompactionCoordinator`: arming at turn end,
cancelling on activity, the generation check, the live-signal probes and the
commit. The threshold path does not call into it except to arm at turn end.

A timer, not the idle-expiry sweep: that sweep ticks every five minutes by default,
which is coarser than the shortest lifetime it would have to beat.

Each arm takes a fresh number from one instance-wide counter, and a fire names
the number it was armed with. Cancelling drops the key's entry, so a timer whose
stretch a later turn, reset or setting change already ended matches nothing and
does nothing, even if its callback was already queued on the loop. Nothing here is persisted: after a gateway restart a
session has no timer until its first turn ends, so the idle clock runs from a
moment after the restore and never from one before it.

The clock and the scheduler are injected so tests drive the timer without real
time. The default scheduler is the running loop's ``call_later``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew.agent_sdk.backends import idle_compaction_delay_secs, prompt_cache_lifetime_secs
from kiro_crew.platform_compat import boottime_now

if TYPE_CHECKING:
    from kiro_crew.session_compaction import CompactionCoordinator

logger = logging.getLogger("kiro_crew.session")

#: The only session kind Phase 1 compacts while idle. Channel threads, cron jobs,
#: sub-agents, task-runner steps and the background runtimes are left alone.
IDLE_COMPACTION_KEY_PREFIX = "dashboard:"

#: The ``trigger`` label of a compaction this module started, on
#: ``kirocrew.context.compactions``. Such a compaction never resets the session or
#: drops its conversation: a reported failure arms the cooldown and stops.
COMPACT_TRIGGER_IDLE = "idle"

#: ``session.idle_compaction_min_context_pct`` when the live config has none.
_DEFAULT_MIN_CONTEXT_PCT = 50.0

#: How long :meth:`IdleCompaction.quiesce` waits for the harness to acknowledge
#: the cancel of an idle ``/compact`` that reported no status.
IDLE_COMPACT_CANCEL_ACK_SECS = 5.0
#: How long :meth:`IdleCompaction.quiesce` then reads for that compaction's late
#: status, at most; the session's compaction budget caps it when lower.
IDLE_COMPACT_DRAIN_SECS = 10.0
#: Slack on each of those waits before the outer bound gives up on the call.
_QUIESCE_BACKSTOP_SECS = 2.0

#: The threshold-check declines after which a turn end arms the idle timer: the
#: session is compactable later, and nothing compacted it on this reading.
_ARMING_DECLINES = frozenset({"below_threshold", "cooldown"})


class TimerHandle(Protocol):
    def cancel(self) -> Any: ...  # noqa: E704


#: ``schedule(delay_secs, callback)`` runs *callback* once after *delay_secs* and
#: returns a handle whose ``cancel()`` stops it.
Scheduler = Callable[[float, Callable[[], None]], TimerHandle]
#: ``fire(key, generation)``: the idle check and compaction a due timer starts.
FireCallback = Callable[[str, int], Awaitable[Any]]


def _loop_call_later(delay: float, callback: Callable[[], None]) -> TimerHandle:
    return asyncio.get_running_loop().call_later(delay, callback)


def provider_prompt_cache_lifetime(provider: Any) -> float | None:
    """The prompt-cache lifetime the backend behind *provider* runs with, or ``None``.

    The backend id is the ACP client's own. The environment is the one its
    process was launched with, as Crew builds it: the gateway's environment,
    then ``agent.env`` (the client's snapshot of it), then the session's own
    variables, later layers winning. A provider that is not an ACP provider,
    or whose backend id is not a string, has no known lifetime.
    """
    try:
        client = getattr(provider, "client", None)
        backend = getattr(client, "backend", None)
        if not isinstance(backend, str):
            return None
        env: dict[str, str] = dict(os.environ)
        for layer in (
            getattr(client, "_agent_env_snapshot", None),
            getattr(client, "_extra_env", None),
        ):
            if isinstance(layer, Mapping):
                env.update({str(k): str(v) for k, v in layer.items()})
        return prompt_cache_lifetime_secs(backend, env)
    except Exception:
        logger.debug("prompt-cache lifetime unreadable for %r", provider, exc_info=True)
        return None


@dataclass(slots=True)
class _Armed:
    generation: int
    due_at: float
    handle: TimerHandle


def _no_boot_clock() -> float | None:
    return None


class IdleCompactionTimers:
    """One cancellable idle timer per session key, with a generation fence."""

    def __init__(
        self,
        *,
        fire: FireCallback,
        background_tasks: set[asyncio.Task[Any]],
        clock: Callable[[], float] = time.monotonic,
        schedule: Scheduler | None = None,
        boot_clock: Callable[[], float | None] | None = None,
    ) -> None:
        self._fire = fire
        self._background_tasks = background_tasks
        self.clock = clock
        #: A clock that keeps counting while the host is suspended (``None`` where
        #: the platform has none). ``clock`` pauses through a suspend, so the two
        #: read together across one stretch give the time spent suspended. An
        #: injected ``clock`` gets no boot clock unless one is passed with it, so
        #: a fake clock is never compared against the real one.
        if boot_clock is None:
            boot_clock = boottime_now if clock is time.monotonic else _no_boot_clock
        self.boot_clock = boot_clock
        #: Each armed key's ``(clock, boot_clock)`` reading at its arm, dropped
        #: with the generation so it stays bounded by the keys in a stretch.
        self._arm_readings: dict[str, tuple[float, float | None]] = {}
        self._schedule: Scheduler = schedule or _loop_call_later
        #: Each key's latest arm, held only from its ``arm`` to its ``cancel`` so
        #: the table stays bounded by the keys in an idle stretch. Generations come
        #: from one counter shared by every key, so a key re-armed after its entry
        #: was dropped never reuses a number a stale fire still carries.
        self._generations: dict[str, int] = {}
        self._last_generation = 0
        self._armed: dict[str, _Armed] = {}

    def generation(self, key: str) -> int:
        return self._generations.get(key, 0)

    def is_current(self, key: str, generation: int) -> bool:
        """Whether *generation* is still *key*'s latest arm."""
        return self._generations.get(key, 0) == generation

    def armed(self, key: str) -> bool:
        return key in self._armed

    def due_at(self, key: str) -> float | None:
        armed = self._armed.get(key)
        return None if armed is None else armed.due_at

    def arm(self, key: str, delay_secs: float) -> int:
        """Start *key*'s idle stretch: replace any timer with one due in *delay_secs*."""
        generation = self._advance(key)
        now = self.clock()
        due_at = now + delay_secs
        self._arm_readings[key] = (now, self.boot_clock())
        handle = self._schedule(delay_secs, lambda: self._due(key, generation))
        self._armed[key] = _Armed(generation, due_at, handle)
        return generation

    def cancel(self, key: str) -> bool:
        """End *key*'s idle stretch. True when a timer was still pending.

        The key's generation is dropped even when the timer already fired, so a
        fire whose idle check is still awaiting reads as stale once activity
        (a claim, reset, recycle or removal) cancels it, and the entry does not
        outlive the stretch.
        """
        armed = self._armed.pop(key, None)
        self._generations.pop(key, None)
        self._arm_readings.pop(key, None)
        if armed is None:
            return False
        armed.handle.cancel()
        return True

    def cancel_all(self) -> int:
        """End every idle stretch, as turning the setting off does.

        A fire whose callback already ran has left ``_armed`` but may still be
        awaiting its probe; dropping every generation, not only the armed ones,
        makes that fire fail its generation check too.
        """
        keys = list(self._armed)
        for key in keys:
            self.cancel(key)
        self._generations.clear()
        self._arm_readings.clear()
        return len(keys)

    def suspended_since_arm(self, key: str) -> float:
        """Seconds the host spent suspended since *key* was armed, 0 when unknown."""
        reading = self._arm_readings.get(key)
        boot_now = self.boot_clock()
        if reading is None or boot_now is None:
            return 0.0
        mono_at, boot_at = reading
        if boot_at is None:
            return 0.0
        return max(0.0, (boot_now - boot_at) - (self.clock() - mono_at))

    def _advance(self, key: str) -> int:
        self._last_generation += 1
        generation = self._last_generation
        self._generations[key] = generation
        return generation

    def _due(self, key: str, generation: int) -> None:
        armed = self._armed.get(key)
        if armed is None or armed.generation != generation:
            return
        remaining = armed.due_at - self.clock()
        if remaining > 0:
            # A scheduler may run a callback early (a coarse loop clock); the
            # stretch is only over when the injected clock says so.
            armed.handle = self._schedule(remaining, lambda: self._due(key, generation))
            return
        # Consumed before the check runs: one fire per stretch, whatever it decides.
        self._armed.pop(key, None)
        task = asyncio.ensure_future(self._fire(key, generation))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)


def provider_request_floor(provider: Any) -> float | None:
    """A monotonic time no later than the start of *provider*'s last model request.

    Read from the provider's prompt delivery (``EssentialDelivery.request_floor``),
    which every provider stream passes through. ``None`` when the provider has
    streamed nothing or exposes no such record.
    """
    delivery = getattr(provider, "essential_delivery", None)
    floor = getattr(delivery, "request_floor", None)
    if isinstance(floor, bool) or not isinstance(floor, (int, float)):
        return None
    return float(floor)


def idle_delay_for(provider: Any, lifetime: Callable[[Any], float | None]) -> float | None:
    """Seconds after the last request's start at which an idle chat on *provider*
    is compacted, or ``None`` to skip."""
    secs = lifetime(provider)
    if secs is None or secs <= 0:
        return None
    return idle_compaction_delay_secs(secs)


def _compaction_unmanaged(provider: Any) -> bool:
    """Whether *provider* names its backend as compacted by nobody.

    Deferred import: ``session_compaction`` imports this module at load, so this
    module reads the coordinator's own predicate only when a fire asks.
    """
    from kiro_crew.session_compaction import _compaction_unmanaged_backend

    return _compaction_unmanaged_backend(provider) is not None


class IdleCompaction:
    """The ``session.idle_compaction`` trigger of one compaction coordinator.

    Reads the coordinator's owner, state and deps, and commits through the
    coordinator's ``_set_compacting`` and the owner's ``_compact_session`` seam,
    so a threshold and an idle compaction share one dedupe set and one
    compaction path.
    """

    def __init__(self, coordinator: CompactionCoordinator) -> None:
        self._coordinator = coordinator
        #: Keys already told their backend has no lifetime (``_log_skipped_backend``).
        self._skip_logged: set[str] = set()
        deps = coordinator._deps
        background = getattr(coordinator._owner, "_background_tasks", None)
        self.timers = IdleCompactionTimers(
            fire=self.fire,
            background_tasks=background if isinstance(background, set) else set(),
            clock=deps.idle_clock,
            schedule=deps.idle_schedule,
        )

    def setting(self) -> tuple[bool, float]:
        """Whether idle compaction is on, and its minimum context percentage."""
        session_cfg = getattr(getattr(self._coordinator._owner, "_cfg", None), "session", None)
        enabled = getattr(session_cfg, "idle_compaction", False) is True
        min_pct = getattr(session_cfg, "idle_compaction_min_context_pct", None)
        if not isinstance(min_pct, (int, float)):
            min_pct = _DEFAULT_MIN_CONTEXT_PCT
        return enabled, float(min_pct)

    def arm_after_turn(self, key: str, provider: Any, pct: float, decline: str | None) -> None:
        """Start *key*'s idle stretch at this turn end, or leave it unarmed.

        Every turn end first ends the previous stretch. A new one is armed only
        when the setting is on, the key is a dashboard chat, the threshold check
        just declined for a reason that leaves the session compactable later
        (below the threshold, or in its cooldown), the reading is confirmed and at
        least the minimum, the backend's cache lifetime is known, and the fire
        time counted from the last request's start has not already passed. A threshold
        compaction that started on this reading is never followed by an idle one
        for the same stretch.
        """
        co = self._coordinator
        self.timers.cancel(key)
        enabled, min_pct = self.setting()
        if not enabled or not key.startswith(IDLE_COMPACTION_KEY_PREFIX):
            return
        fire_after = idle_delay_for(provider, co._deps.prompt_cache_lifetime_secs)
        if fire_after is None:
            self._log_skipped_backend(key, provider)
            return
        if decline not in _ARMING_DECLINES or key in co.state.compacting:
            return
        if pct < min_pct or co._deps.context_pct_is_unknown(provider):
            return
        floor = provider_request_floor(provider)
        if floor is None:
            co._deps.logger.info(
                "Session %s idle compaction not armed: no start time for its last request", key
            )
            return
        # The cache lifetime runs from the start of the last request, and the
        # floor is no later than that start, so the fire lands no later than the
        # margin before expiry however long the final response streamed.
        delay = floor + fire_after - self.timers.clock()
        if delay <= 0:
            co._deps.logger.info(
                "Session %s idle compaction not armed: its last request started %.0fs "
                "ago, past the %.0fs fire time",
                key,
                fire_after - delay,
                fire_after,
            )
            return
        self.timers.arm(key, delay)
        co._deps.logger.info(
            "Session %s idle compaction armed for %.0fs (context at %.0f%%)", key, delay, pct
        )

    def _log_skipped_backend(self, key: str, provider: Any) -> None:
        """Say once per session that its backend has no known cache lifetime.

        Without it a user who turned the setting on sees nothing happen on such a
        backend (KAS, for one) and no reason why. Once per key, not per turn.
        """
        if key in self._skip_logged:
            return
        self._skip_logged.add(key)
        backend = getattr(getattr(provider, "client", None), "backend", None)
        self._coordinator._deps.logger.info(
            "Session %s idle compaction does not run on backend %s: no known "
            "prompt-cache lifetime",
            key,
            backend if isinstance(backend, str) and backend else "unknown",
        )

    def cancel(self, key: str) -> bool:
        """End *key*'s idle stretch: a claim, reset, recycle or removal is activity."""
        cancelled = self.timers.cancel(key)
        folded = self._coordinator._owner._fold_key(key)
        if folded != key:
            cancelled = self.timers.cancel(folded) or cancelled
        return cancelled

    def forget(self, key: str) -> bool:
        """End *key*'s idle stretch and drop what it retains: the session is gone.

        The reset, recycle and removal path, so ``_skip_logged`` holds only live
        sessions and the session that next takes the key logs its skip once again.
        """
        folded = self._coordinator._owner._fold_key(key)
        self._skip_logged.discard(key)
        self._skip_logged.discard(folded)
        return self.cancel(key)

    def cancel_all(self) -> int:
        """End every idle stretch, as turning ``session.idle_compaction`` off does."""
        return self.timers.cancel_all()

    async def quiesce(self, key: str, session: Any) -> str | None:
        """Cancel an idle ``/compact`` that reported no status, and drain its late one.

        The caller holds the turn permit, so nothing else reads the client while
        this does. The cancel goes through the provider's own ``cancel``; the late
        status through its ``wait_for_compaction``, which also drains the
        post-compaction metadata after a ``completed``. The cancel acknowledgement
        has a short fixed bound; the drain reads for at most
        ``IDLE_COMPACT_DRAIN_SECS``, capped by the configured compaction budget
        (``session.compact_wait_secs``). A user who returns during the drain
        waits behind it, as RFC 5.5 accepts for any in-flight idle compaction.
        A silent harness is never answered with a teardown. Returns
        ``"completed"`` or ``"failed"`` when the harness reported one, and
        ``None`` when it reported neither.
        """
        log = self._coordinator._deps.logger
        provider = session.provider
        try:
            await asyncio.wait_for(
                provider.cancel(wait_ack_timeout=IDLE_COMPACT_CANCEL_ACK_SECS),
                timeout=IDLE_COMPACT_CANCEL_ACK_SECS + _QUIESCE_BACKSTOP_SECS,
            )
        except (Exception, asyncio.TimeoutError):
            log.debug("Session %s idle /compact cancel failed", key, exc_info=True)
        # The drain is short: a status it misses is dropped by the stale mark
        # the caller arms, so a returning user waits at most this bound past
        # the compaction budget. Capped by the threshold path's own
        # ``session.compact_wait_secs`` resolver for a lower configured budget.
        drain_budget = min(
            IDLE_COMPACT_DRAIN_SECS, float(self._coordinator._deps.compact_wait_timeout_secs())
        )
        try:
            result = await asyncio.wait_for(
                provider.wait_for_compaction(timeout=drain_budget),
                timeout=drain_budget + _QUIESCE_BACKSTOP_SECS,
            )
        except (Exception, asyncio.TimeoutError):
            log.debug("Session %s idle /compact drain failed", key, exc_info=True)
            return None
        status = result.get("type") if isinstance(result, dict) else None
        return status if status in ("completed", "failed") else None

    async def fire(self, key: str, generation: int) -> None:
        """The timer's callback: run the idle check for the arm it fired for."""
        log = self._coordinator._deps.logger
        try:
            outcome = await self.compact_if_idle(key, generation=generation)
        except Exception:
            log.exception("Session %s idle compaction failed", key)
            return
        log.info("Session %s idle compaction: %s", key, outcome)

    def _gate(self, key: str, provider: Any) -> str | None:
        """The threshold gate's rungs without the threshold, then the idle-only refusals.

        ``_gate_rung(skip_threshold=True)`` answers ``cc_managed``,
        ``compact_unsupported``, ``unconfirmed``, ``in_progress`` and ``cooldown``
        exactly as the threshold path does. A pending verdict waits for its next
        confirmed reading, and a backend no one compacts is refused, so the idle
        trigger never reaches the recycle arm.
        """
        co = self._coordinator
        pct = provider.context_usage_pct()
        rung = co._gate_rung(key, provider, pct, log=False, skip_threshold=True)
        if rung is not None:
            return rung
        if key in co.state.pending_verdict:
            return "pending_verdict"
        if _compaction_unmanaged(provider):
            return "unmanaged"
        return None

    def live_activity(self, key: str, session: Any) -> str | None:
        """The first live signal on *key* that makes it not idle, else ``None``.

        Synchronous, so the caller can read it and commit in the same loop step.
        A probe that raises, or a dashboard key with no activity probe installed,
        answers busy: an idle compaction is optional and must never land on work
        it could not see.
        """
        owner = self._coordinator._owner
        semaphore = getattr(session, "semaphore", None)
        if semaphore is None or semaphore.locked():
            return "turn"
        holder = getattr(session, "turn_owner", None)
        if isinstance(holder, asyncio.Task) and not holder.done():
            return "turn"
        if getattr(session, "lifecycle_turn_active", False) is True:
            return "turn"
        if len(getattr(session, "queue", ()) or ()) > 0:
            return "queue"
        if key in getattr(owner, "_recycling", {}):
            return "recycling"
        try:
            active = getattr(session.provider, "has_active_turn", None)
            if callable(active) and active() is True:
                return "turn"
            injection = getattr(owner, "_has_pending_injection", None)
            if callable(injection) and injection(key):
                return "injection"
            probe = getattr(owner, "_idle_compaction_blocker", None)
            blocker = probe(key) if callable(probe) else "no_activity_probe"
        except Exception:
            self._coordinator._deps.logger.debug(
                "Session %s idle activity probe failed", key, exc_info=True
            )
            return "probe_error"
        return blocker if isinstance(blocker, str) and blocker else None

    def _cache_expired_ago(self, key: str, provider: Any) -> float | None:
        """Seconds since *provider*'s prompt cache expired, or ``None`` while it lives.

        The anchor is the arm's own: the last request's start plus the cache
        lifetime, on the injected clock. A fire the event loop delivered late
        (a paused loop, a suspended host) would otherwise compact after the cache
        is gone, which saves nothing and announces a reason that is false. An
        anchor that cannot be read is not evidence of expiry.
        """
        floor = provider_request_floor(provider)
        lifetime = self._coordinator._deps.prompt_cache_lifetime_secs(provider)
        if floor is None or lifetime is None or lifetime <= 0:
            return None
        # The anchor and the injected clock both pause while the host is
        # suspended; the cache's lifetime does not, so add the suspended time.
        ago = self.timers.clock() - (floor + lifetime) + self.timers.suspended_since_arm(key)
        return ago if ago >= 0 else None

    async def compact_if_idle(self, key: str, *, generation: int | None = None) -> str:
        """Compact *key* once because it went idle, or say why not.

        Called by the idle timer (*generation* is the arm it fired for) and
        returns the outcome of the compaction, or the name of the check that
        declined it. Any decline ends the idle stretch; the next turn re-arms.

        The sub-agent probe is the one asynchronous check and runs first. Every
        check after it -- the setting and generation, the gate rungs, the backend,
        the live-activity signals -- runs in one loop step with the commit to
        ``state.compacting``, so a turn that claims the session cannot slip in
        between the last check and the commit, and a threshold compaction on the
        same key is deduplicated by the same set.

        The compaction itself is the one the threshold uses, with the idle
        trigger: in place, holding the turn permit for one ``/compact``. A
        reported failure takes only the cooldown, never a recycle or a reset. A
        ``/compact`` that reports nothing is cancelled and drained under the
        permit for up to the compaction budget; if it still reports nothing the
        session is kept untouched and the cooldown armed
        (``CompactionCoordinator._settle_idle_unconfirmed``). Last before the
        commit, a fire that arrives after the cache has already expired is
        declined as ``expired``.
        """
        co = self._coordinator
        owner = co._owner
        key = owner._fold_key(key)
        try:
            attached = getattr(owner, "_has_attached_subagents", lambda _k: False)(key)
            if inspect.isawaitable(attached):
                attached = await attached
        except Exception:
            co._deps.logger.debug("Session %s sub-agent probe failed", key, exc_info=True)
            attached = True
        if attached:
            return "subagents"
        # No await from here to the commit.
        enabled, min_pct = self.setting()
        if not enabled:
            return "disabled"
        if generation is not None and not self.timers.is_current(key, generation):
            return "stale"
        session = owner._sessions.get(key)
        if session is None:
            return "absent"
        provider = session.provider
        refused = self._gate(key, provider)
        if refused is not None:
            return refused
        pct = provider.context_usage_pct()
        if pct < min_pct:
            return "below_minimum"
        busy = self.live_activity(key, session)
        if busy is not None:
            return f"busy:{busy}"
        expired_ago = self._cache_expired_ago(key, provider)
        if expired_ago is not None:
            co._deps.logger.info(
                "Session %s idle compaction declined: its prompt cache expired %.0fs ago",
                key,
                expired_ago,
            )
            return "expired"
        co._deps.logger.warning(
            "Session %s compacting while idle — context at %.0f%% before cache expiry", key, pct
        )
        co._set_compacting(key, True)
        co.state.compaction_triggers[key] = COMPACT_TRIGGER_IDLE
        current = asyncio.current_task()
        if current is not None:
            co.state.compaction_tasks[key] = current
        # The trigger recorded above is what the owner seam's arms read, so the
        # seam keeps its two-argument shape for every caller and patch.
        return await owner._compact_session(key, pct)
