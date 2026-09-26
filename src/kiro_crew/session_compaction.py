"""Context-compaction policy and execution for :mod:`kiro_crew.session`.

The coordinator deliberately depends on a small owner facade instead of
importing ``kiro_crew.session``.  Session allocation and teardown still own the
registry, its lock, and the exact-identity ``_recycling`` marker; compaction
borrows those seams without becoming a second session manager.

One observation rides this boundary and decides nothing: ``_compact_session``
starts the ``compaction.keep`` shadow measurement
(``decisions/points/compaction_keep.py``) as an unawaited background task before it
picks an arm, so a consented install learns which tool calls an oracle would have
kept while the compaction itself runs byte-identically.

Calls between formerly patchable ``SessionManager`` methods go back through
the owner.  Besides preserving the public/private monkeypatch surface during
the extraction, that keeps one authoritative place for lifecycle operations
such as guarded reset and queue retirement.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew.context_budget import replay_budget_chars, startup_context_chars
from kiro_crew.metrics.events import CONTEXT_COMPACTIONS, emit_counter
from kiro_crew.metrics.sessions import END_REASON_RECYCLED, record_session_ended
from kiro_crew.session_compaction_methods import (
    COMPACTION_METHOD_NATIVE,
    COMPACTION_METHOD_SHAKE,
    COMPACTION_METHOD_SOFT,
    OUTCOME_INSUFFICIENT,
    OUTCOME_UNAVAILABLE,
    SEED_BUDGET_DIVISOR,
    SEED_NOTHING,
    SEED_TAIL_ONLY,
    SEED_WRITTEN,
    estimate_tokens,
    method_sufficient,
    project_pct_after,
    rotation_ladder,
)

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider


#: The session was compacted: its history was summarized and it kept going.
COMPACT_OUTCOME_COMPACTED = "compacted"
#: The session was REPLACED after a compaction it COULD have had failed -- the
#: in-place ``/compact`` timed out or errored, so the provider was recycled instead.
#: A distinct value from ``compacted`` because the two are equally "successful" to a
#: caller that only needs to know the session has headroom again, and telling a user
#: they were compacted when they were replaced is the exact class of untruth this
#: vocabulary exists to prevent.
COMPACT_OUTCOME_RECYCLED = "recycled"
#: The session was replaced because NOTHING could have compacted it: the backend is a
#: member of ``ACP_BACKENDS_CONTEXT_RECYCLE`` and no compaction reaches it from either
#: side. Separate from ``recycled`` because only this one may say the backend cannot
#: compact -- a kiro-cli or opencode session reaches the value above, and claiming a
#: missing capability there would be false about a harness that has it and merely
#: failed once.
COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE = "restarted_uncompactable"


class CompactCallback(Protocol):
    """Surface hook fired once per compaction verdict.

    *outcome* says WHICH arm ran (compacted in place, recycled, or recycled
    because the backend cannot compact at all); *method* names what ran: a
    rotation method (``soft``, ``shake``) or ``native``. The surface needs
    both because a rotation, an in-place summary and a plain recycle reach it
    through the same call with ``success=True``, and the notice it posts has
    to say which one the user now lives with. *all_kept* is ``True`` for the
    one rotation that dropped nothing: a ``shake`` whose seed writer answered
    ``SEED_NOTHING`` (the tail covers every row) recycles as ``soft``, and
    ``method`` still says ``soft`` because that is what ran, but the notice
    for it says the agent carried the whole conversation, not that the earlier
    turns are gone. ``False`` for every other verdict.
    """

    async def __call__(  # noqa: E704
        self,
        key: str,
        pct: float,
        *,
        success: bool,
        outcome: str = COMPACT_OUTCOME_COMPACTED,
        method: str,
        all_kept: bool = False,
    ) -> None: ...


class SeedWriter(Protocol):
    """Surface hook behind the ``shake`` method.

    The coordinator owns no transcript; the surface that does (the dashboard
    slot and its conversation log) writes the seed row the successor session
    is seeded from. ``supports`` is asked BEFORE the rotation is projected and
    must be cheap and synchronous (no transcript read): it answers whether this
    surface holds a transcript it can digest for *key* at all. A ``False``
    there is ``unavailable`` for the whole method, so the walk hands to
    ``native`` instead of letting an insufficient projection slide to ``soft``
    on a surface where nobody would see the loss. The call itself runs with
    the turn semaphore held, so the rows it reads are quiescent.
    *window_tokens* is the provider's window: the surface sizes the verbatim
    tail from it with the replay's own budget function, the same figure the
    coordinator projected with. It answers one of ``SEED_WRITTEN``;
    ``SEED_NOTHING`` when the complete conversation fits the tail, so the
    coordinator recycles as ``soft`` with ``all_kept=True``;
    ``SEED_TAIL_ONLY`` when no seed was written but rows fall outside the tail,
    so the coordinator recycles as ``soft`` with ``all_kept=False``; or
    ``SEED_UNSUPPORTED`` when the surface is gone at write time.
    """

    def supports(self, key: str) -> bool: ...  # noqa: E704

    async def __call__(self, key: str, method: str, window_tokens: int) -> str: ...  # noqa: E704


@dataclass(slots=True)
class CompactionState:
    """Mutable state owned exclusively by the compaction boundary."""

    compacting: set[str] = field(default_factory=set)
    #: Keys whose in-flight recycle is happening because NOTHING could compact them,
    #: as opposed to because a compaction failed. Written by ``_recycle_held`` and
    #: consumed by ``_fire_compact_callback`` at the end of the same recycle, so it
    #: holds at most the handful of keys recycling right now.
    uncompactable_recycles: set[str] = field(default_factory=set)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    pending_verdict: dict[str, float] = field(default_factory=dict)
    #: Per-session threshold overrides (folded key -> pct). A key absent here
    #: falls back to the published global (``cfg.session.autocompact_pct``).
    #: Deliberately independent of ``_sessions`` membership: an override is a
    #: user preference on the conversation, so it survives session resets and
    #: recycles, and is re-seeded from slot persistence after a restart.
    pct_overrides: dict[str, float] = field(default_factory=dict)
    on_compacted: CompactCallback | None = None
    #: Sessions being recycled ON PURPOSE by a rotation method (folded key ->
    #: method name), held for exactly the span of ``_recycle_held``. The same
    #: recycle primitive serves the native failure path, so this is what tells
    #: the success counter a rotation apart from a replaced provider.
    rotating: dict[str, str] = field(default_factory=dict)
    #: Keys whose in-flight rotation dropped nothing: the seed writer answered
    #: ``SEED_NOTHING`` (the tail covers every row), so the recycle runs as
    #: ``soft`` and ``rotating`` says ``soft``, which is what the counter and the
    #: verdict need, but the notice must not read ``soft``'s fate. Held for the
    #: same span as ``rotating``: added beside it, discarded where it is popped,
    #: and read by ``_fire_compact_callback`` as the callback's ``all_kept``.
    rotation_kept_all: set[str] = field(default_factory=set)
    #: Rotations awaiting their verdict (folded key -> method name). A rotation
    #: is judged before it runs, by a projection; the successor's first
    #: confirmed reading is the after-the-fact check, and it settles through the
    #: same ``pending_verdict`` the native path uses. This map says which of
    #: those pending verdicts a rotation armed, so an ineffective one can hold.
    pending_rotation: dict[str, str] = field(default_factory=dict)
    #: Sessions whose last rotation was judged ineffective (the successor still
    #: sat inside the threshold band), mapped to what that rotation's projection
    #: read: the configured method, the window and the threshold. The projection
    #: reads those three and fixed budgets, never the session's content, so
    #: while all three hold, every later crossing would project the same and
    #: rotate into the same band. The ladder runs ``native`` for the key until
    #: one of them changes.
    rotation_hold: dict[str, tuple[str, int, float]] = field(default_factory=dict)
    seed_writer: SeedWriter | None = None


@dataclass(frozen=True, slots=True)
class CompactionDeps:
    """Runtime seams supplied by ``session.py`` when the coordinator is wired.

    The callable values are intentional.  Existing tests and internal
    companions patch names in ``kiro_crew.session`` after constructing a
    manager, so the owner should inject small forwarding lambdas rather than
    frozen snapshots of those names.  ``get_recorder`` remains part of the
    dependency boundary even though the current compaction path emits no
    metric; it prevents a later metric from re-introducing the metrics/session
    import edge.
    """

    logger: logging.Logger
    is_claude_backend: Callable[[Any], bool]
    is_cc_managed: Callable[[Any], bool]
    get_recorder: Callable[[], Any]
    context_pct_is_unknown: Callable[[LLMProvider], bool]
    unlink_session_queue: Callable[[Any], None]
    compact_wait_timeout_secs: Callable[[], float]
    compact_result_wait_secs: Callable[[float], float]
    context_warn_margin_pct: float
    compact_result_wait_margin_secs: float
    compact_failure_cooldown_secs: float
    compact_min_effect_pct_points: float
    post_compact_reset_pct: float


class _CompactionOwner(Protocol):
    """Structural contract implemented by the ``SessionManager`` facade."""

    _cfg: Any
    _sessions: dict[str, Any]
    _lock: asyncio.Lock
    _recycling: dict[str, Any]
    _session_map: Any
    _background_tasks: set[asyncio.Task[Any]]

    def _fold_key(self, key: str) -> str: ...

    def _advance_session_generation(self, key: str) -> int: ...

    def _trigger_compaction(
        self, key: str, reason: str, pct: float, provider: LLMProvider
    ) -> str | None: ...

    def _compaction_gate_decision(
        self, key: str, provider: LLMProvider, pct: float
    ) -> str | None: ...

    async def _compact_session(self, key: str, pct: float) -> str: ...

    async def _compact_in_place(self, key: str, session: Any, pct: float) -> str: ...

    async def _recycle_held(
        self, key: str, session: Any, pct: float, *, uncompactable: bool = False
    ) -> None: ...

    async def _recycle_unmanaged(self, key: str, session: Any, pct: float) -> str: ...

    def _settle_compact_cooldown(
        self, key: str, provider: LLMProvider, pct_before: float
    ) -> bool: ...

    def _judge_compact_effect(self, key: str, pct_before: float, pct_after: float) -> bool: ...

    async def _reset_still_critical(
        self, key: str, pct_before: float, pct_after: float, *, expect: Any | None
    ) -> bool: ...

    async def _fire_compact_callback(self, key: str, pct: float, *, success: bool) -> None: ...

    def mark_needs_reinjection(self, key: str) -> None: ...

    async def reset(
        self,
        key: str,
        *,
        expect_session: Any | None = None,
        skip_if_busy: bool = False,
        clear_conversation: bool = False,
        ends_conversation: bool = False,
    ) -> bool: ...


def _compact_unsupported_backend(provider: LLMProvider) -> str | None:
    """Backend id this provider names as unable to serve ``/compact``, else None.

    The same capability the manual entry points read, asked for the
    automatic one.  The property is spelled ``manual_`` because the manual
    command was its first consumer, but its ANSWER is a property of the
    BACKEND -- ``ACP_BACKENDS_COMPACT`` membership -- not of the entry point,
    so it is the right question here too: a backend that cannot act on the
    ``/compact`` prompt cannot act on it whoever sent it.

    Read with the consumption contract the ABC declares: only a non-empty
    ``str`` counts.  That guard is load-bearing rather than defensive -- the
    compaction suite drives this gate with bare ``object()`` and ``MagicMock``
    providers, and a truthy attribute read on either must not be mistaken for
    a positively named unsupported backend.
    """
    backend = getattr(provider, "manual_compact_unsupported_backend", None)
    return backend if isinstance(backend, str) and backend else None


def _compaction_unmanaged_backend(provider: LLMProvider) -> str | None:
    """Backend id this provider names as compacted by NOBODY, else None.

    The strictly narrower question :func:`_compact_unsupported_backend` leaves
    open.  That one says Crew may not send a ``/compact`` prompt; this one says
    what happens instead.  A harness that summarizes on its own initiative and
    reports it (``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION``) answers ``None``
    here even though it answers a backend id there, because its context IS
    bounded -- Crew simply is not the one bounding it.  A harness that answers a
    backend id to BOTH has no compaction anywhere Crew can see, so skipping its
    threshold is not a decline but a leak.

    Read with the same consumption contract as its sibling, and for the same
    reason: only a non-empty ``str`` counts, so the ``object()`` and
    ``MagicMock`` providers the compaction suite drives this gate with cannot
    have a truthy attribute mistaken for a positive claim that nothing compacts.
    """
    backend = getattr(provider, "compaction_unmanaged_backend", None)
    return backend if isinstance(backend, str) and backend else None


def _compaction_harness_managed(provider: LLMProvider) -> bool:
    """True when the harness POSITIVELY claims it bounds its own context.

    The two reasons a decline happens, told apart. A member of
    ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`` answers a backend id to
    :func:`_compact_unsupported_backend` and ``None`` to
    :func:`_compaction_unmanaged_backend`, and so does a backend in NO compaction
    set at all -- the two are indistinguishable from those two answers alone,
    which is why this asks the provider its own third question instead of
    inferring from them. A provider that does not answer it is taken as
    harness-managed, matching the ABC default: that is the reading that changes
    no log line.
    """
    claimed = getattr(provider, "compaction_self_managed", None)
    return claimed is not False


class CompactionCoordinator:
    """Coordinate context compaction while ``SessionManager`` remains facade."""

    def __init__(
        self,
        owner: _CompactionOwner,
        deps: CompactionDeps,
        *,
        state: CompactionState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self.state = state

    def check_context_usage(self, key: str, provider: LLMProvider) -> float:
        """Record usage and fire background compaction at the configured threshold."""
        owner = self._owner
        key = owner._fold_key(key)
        pct = provider.context_usage_pct()

        # The prompt counter also drives the background-session recycle policy.
        session = owner._sessions.get(key)
        if session:
            session.prompt_count += 1
            self._record_floor(key, session, provider, pct)

        # Go through the owner so patches on SessionManager._trigger_compaction
        # keep intercepting the call after this extraction.
        decline = owner._trigger_compaction(key, f"context at {pct:.0f}%", pct, provider)

        if decline == "cc_managed":
            if pct > 0:
                self._deps.logger.info("Session %s context at %.0f%% (CC-managed)", key, pct)
        elif decline == "below_threshold":
            warn_at = self.effective_autocompact_pct(key) - self._deps.context_warn_margin_pct
            if warn_at > 0 and pct >= warn_at:
                self._deps.logger.warning("Session %s context at %.0f%%", key, pct)
            elif pct > 0:
                self._deps.logger.info("Session %s context at %.0f%%", key, pct)
        return pct

    def _record_floor(self, key: str, session: Any, provider: LLMProvider, pct: float) -> None:
        """Pin a session that started with no history to its first confirmed reading.

        ``floor_pending`` is set on the session whose cold start consumed a
        replay suppression (``SessionManager.consume_replay_suppression``), so
        it started with no conversation.  Its first CONFIRMED reading is the
        floor a fresh session on this key reads before anyone has said
        anything.  Recorded once per provider and never lowered: a reading that
        arrives after telemetry confirms may include turns that ran meanwhile,
        which only over-states the floor -- and an over-stated floor declines a
        reset, never forces one.  The flag lives on the session so it dies with
        it; nothing here outlives the registry entry.  Bookkeeping, not a gate:
        it decides nothing about compaction and lives outside the ladder.
        """
        if not getattr(session, "floor_pending", False):
            return
        if pct <= 0 or self._deps.context_pct_is_unknown(provider):
            return
        session.floor_pending = False
        if getattr(session, "floor_pct", None) is None:
            session.floor_pct = pct
            self._deps.logger.info(
                "Session %s context floor recorded at %.0f%% (fresh session, no history)",
                key,
                pct,
            )

    async def compact_if_needed(self, key: str) -> str:
        """Await a compaction attempt before a between-turn caller proceeds."""
        owner = self._owner
        key = owner._fold_key(key)
        session = owner._sessions.get(key)
        if session is None:
            return "absent"
        provider = session.provider
        pct = provider.context_usage_pct()

        # Both entry points consume the facade seam so a patched gate has the
        # same effect on awaited and fire-and-forget compaction.
        decline = owner._compaction_gate_decision(key, provider, pct)
        if decline is not None:
            return decline
        self._deps.logger.warning("Session %s compacting — context at %.0f%% (awaited)", key, pct)
        # There is deliberately no await between the membership check in the
        # gate and this commit; that is the event-loop dedup handshake.
        self.state.compacting.add(key)
        return await owner._compact_session(key, pct)

    def set_compact_callback(self, cb: CompactCallback | None) -> None:
        """Register the callback fired after each terminal compact attempt."""
        if self.state.on_compacted is not None and cb is not None:
            self._deps.logger.warning(
                "Compact callback already registered; replacing existing handler"
            )
        self.state.on_compacted = cb

    def set_seed_writer(self, writer: SeedWriter | None) -> None:
        """Register the surface hook the ``shake`` method writes its seed row through."""
        if self.state.seed_writer is not None and writer is not None:
            self._deps.logger.warning("Compaction seed writer already registered; replacing it")
        self.state.seed_writer = writer

    def mark_needs_reinjection(self, key: str) -> None:
        """Flag the live session to restore skill context on its next turn."""
        session = self._owner._sessions.get(self._owner._fold_key(key))
        if session is not None:
            session.needs_context_reinjection = True

    def consume_needs_reinjection(self, key: str) -> bool:
        """Consume the one-shot post-compaction reinjection flag."""
        session = self._owner._sessions.get(self._owner._fold_key(key))
        if session is None or not session.needs_context_reinjection:
            return False
        session.needs_context_reinjection = False
        return True

    def set_autocompact_pct(self, key: str, pct: float | None) -> None:
        """Set or clear (``None``) this session's compaction-threshold override.

        The value is stored as given — range validation belongs to the facade
        (``SessionManager.set_autocompact_pct``), which owns the loader
        constants; this boundary deliberately stays free of config imports.
        """
        key = self._owner._fold_key(key)
        if pct is None:
            self.state.pct_overrides.pop(key, None)
        else:
            self.state.pct_overrides[key] = pct

    def effective_autocompact_pct(self, key: str) -> float:
        """This session's compaction threshold: its override, else the global."""
        return self.state.pct_overrides.get(
            self._owner._fold_key(key), self._owner._cfg.session.autocompact_pct
        )

    def _compaction_gate_decision(self, key: str, provider: LLMProvider, pct: float) -> str | None:
        """Return the first compaction gate decline, in lifecycle order.

        Pending-verdict settlement is a side effect and therefore precedes
        every declining gate.  A deferred reading may arm damping, but its
        ambiguity must never trigger the destructive critical reset.

        ``compact_unsupported`` is deliberately the LAST permanent gate and
        sits BELOW the threshold check: a backend that cannot serve
        ``/compact`` still has a context meter worth reporting, and declining
        above the threshold check would take the per-turn usage line in
        ``check_context_usage`` with it.  Placing it here also means the only
        behaviour that changes for such a backend is the one that cannot work --
        the dispatch itself -- and it changes before ``_compact_session`` is
        ever scheduled, so no ``compacting`` entry, no background task and no
        ``session.semaphore`` acquisition happens for a compaction that could
        only end in the 300s strand.
        """
        baseline = self.state.pending_verdict.get(key)
        if baseline is not None and not self._deps.context_pct_is_unknown(provider):
            del self.state.pending_verdict[key]
            # Append-only the session's log (flag-gated, fail-soft). The OTHER half
            # of the emit in ``_settle_compact_cooldown``: a compaction whose
            # effect was not measurable at the time deferred its verdict to here,
            # and without this line that compaction would never appear in the
            # crew log at all. ``pct`` is the first CONFIRMED reading after it, so
            # it is the honest ``pct_after`` even when it is higher than
            # ``baseline`` -- a deferred reading includes a later turn's growth,
            # which makes ``freed_pct`` negative rather than absent. Recording
            # that beats recording nothing: the entry says a compaction happened
            # and what was measured, and a reader can see the measurement is not
            # a clean before/after because the numbers say so.
            # Deferred, not module-scope: this module is reached from the gateway
            # boot path, and AUTOSDE's no-new-work-on-gateway-boot-path rule asks
            # for a flag-gated subsystem's IMPORT to be gated, not just its use.
            from kiro_crew.crew_log import emit as crew_log_emit

            crew_log_emit.on_compaction_applied(
                crew_log_emit.session_id_of(provider),
                pct_before=baseline,
                pct_after=pct,
            )
            # Ignore the escalation result here: a deferred reading includes a
            # later turn's growth and is only safe for cooldown damping.
            self._owner._judge_compact_effect(key, baseline, pct)
            rotated = self.state.pending_rotation.pop(key, None)
            if rotated is not None and key in self.state.cooldown_until:
                # The judge armed the failure cooldown: the successor of a
                # rotation started inside the band the projection said it would
                # clear. Rotating again would repeat the same arithmetic on the
                # same session, so walks run native until the method, window or
                # threshold the projection read changes.
                self.state.rotation_hold[key] = self._rotation_identity(key, provider)
                self._deps.logger.warning(
                    "Session %s rotation %s left context at %.1f%% (from %.1f%%); "
                    "compactions run native until the method, window or threshold changes",
                    key,
                    rotated,
                    pct,
                    baseline,
                )

        if self._deps.is_cc_managed(provider):
            return "cc_managed"
        if pct < self.effective_autocompact_pct(key):
            return "below_threshold"
        unsupported = _compact_unsupported_backend(provider)
        if unsupported is not None and _compaction_unmanaged_backend(provider) is None:
            # Declining, not recycling, and ONLY for a harness that positively
            # claims the other side of the bargain. A member of
            # ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`` (KAS) summarizes on its
            # own initiative and its ``summarization_completed`` frame calls
            # ``reset_after_compaction()`` on the meter
            # (``acp/session_handle.py``), so the reading this gate just read
            # falls back below the threshold without us acting -- the same
            # relationship ``cc_managed`` encodes for Claude-Code sessions.
            # Recycling such a session would not have been the smaller harm: it
            # destroys a live conversation that was about to shrink on its own.
            #
            # A harness that cannot be handed ``/compact`` AND makes no such
            # claim takes the other arm: it falls through this rung entirely,
            # past ``unconfirmed`` / ``in_progress`` / ``cooldown``, and
            # ``_compact_session`` recycles it. Reaching those three rungs first
            # is the point of falling through rather than returning here -- an
            # unconfirmed reading must not spend a recycle, and neither must a
            # second concurrent trigger.
            #
            # Surfaced the way the gate's own rungs are surfaced: one
            # ``logger.info`` from HERE, as ``unconfirmed``, ``in_progress`` and
            # ``cooldown`` already do -- rather than a branch in
            # ``check_context_usage``, which is where ``cc_managed`` and
            # ``below_threshold`` are phrased because those two also carry the
            # context-meter line. This line REPLACES that per-turn meter line
            # for a declined backend rather than adding to it (the decline
            # matches neither branch there), and it carries strictly more than
            # ``cc_managed``'s: the key, the reading, AND which backend
            # answered. INFO, not WARNING: a standing correct condition is not
            # an anomaly, and every sibling rung is INFO too.
            if _compaction_harness_managed(provider):
                self._deps.logger.info(
                    "Session %s context at %.0f%% -- %s manages compaction itself; "
                    "skipping the /compact dispatch it cannot answer",
                    key,
                    pct,
                    unsupported,
                )
            else:
                # Neither compaction set claims this backend, so nothing here is
                # KNOWN to bound its context. Declining is still the right
                # ACTION -- the alternative ends a conversation, and no harness
                # earns that by never having been classified -- but it is not a
                # standing correct condition the way the branch above is. So it
                # is a WARNING naming the decision that is missing, rather than
                # an INFO sitting beside the rungs that are settled, where the
                # one reading an operator can act on would be invisible.
                self._deps.logger.warning(
                    "Session %s context at %.0f%% -- %s claims neither "
                    "harness-managed compaction nor a context recycle, so nothing "
                    "Crew can see bounds its context; declining rather than "
                    "recycling, and the backend needs a membership in one of the two",
                    key,
                    pct,
                    unsupported,
                )
            return "compact_unsupported"
        if self._deps.context_pct_is_unknown(provider):
            self._deps.logger.info(
                "Session %s context %.0f%% is unconfirmed for this session — "
                "skipping compaction until telemetry reports",
                key,
                pct,
            )
            return "unconfirmed"
        if key in self.state.compacting:
            self._deps.logger.info("Session %s compaction already in progress", key)
            return "in_progress"
        cooldown_until = self.state.cooldown_until.get(key, 0.0)
        if cooldown_until > time.monotonic():
            self._deps.logger.info(
                "Session %s compaction skipped — cooldown active for %.0fs more",
                key,
                cooldown_until - time.monotonic(),
            )
            return "cooldown"
        return None

    def _trigger_compaction(
        self, key: str, reason: str, pct: float, provider: LLMProvider
    ) -> str | None:
        """Commit and schedule a background compact when all gates allow."""
        owner = self._owner
        decline = owner._compaction_gate_decision(key, provider, pct)
        if decline is not None:
            return decline
        self._deps.logger.warning("Session %s compacting — %s", key, reason)
        # Keep check-and-add synchronous so the awaited trigger cannot commit
        # a second attempt in the same event-loop turn.
        self.state.compacting.add(key)
        task = asyncio.create_task(owner._compact_session(key, pct))
        owner._background_tasks.add(task)
        task.add_done_callback(owner._background_tasks.discard)
        return None

    def _shadow_score_compaction(self, key: str) -> None:
        """Start the ``compaction.keep`` shadow measurement. Never raises, never waits.

        Placed at the top of :meth:`_compact_session` because that method is the ONE
        funnel both AUTOMATIC entry points reach -- the per-turn threshold trigger
        (``check_context_usage`` -> ``_trigger_compaction``) and the awaited
        between-turn one (``compact_if_needed``) -- and it is reached BEFORE the
        Claude arm, before ``_recycle_unmanaged`` and before ``_compact_in_place``.
        So every backend is covered and the measurement observes the transcript as it
        stands when the compaction was decided, not after it ran.

        A MANUAL ``/compact`` is excluded by construction, not by a flag: the
        dashboard dispatches that command as an ordinary turn through
        ``provider.stream_command("/compact")`` and never calls this method, so there
        is no manual path for the point to sit on.

        The transcript boundary is captured HERE rather than inside the task, because
        the task runs beside the compaction: see the comment on ``before`` below.

        FIRE AND FORGET, and that is the contract the point's own docstring states:
        the task is created and never awaited, so the compaction proceeds on its own
        coroutine and nothing here can delay it, fail it or change its outcome. The
        worst case is a missing observation.

        Imported inside the function and guarded as a whole: the decisions package is
        an optional subsystem that is off until the owner consents (and this module is
        on the gateway boot path), so its import graph is paid only by a compaction
        that actually fires, and a broken point file cannot cost a session its
        compaction.
        """
        try:
            from kiro_crew.decisions.points.compaction_keep import (
                begin_attempt,
                score_compaction,
            )

            # Minted SYNCHRONOUSLY, before the task exists: the token orders two
            # compactions on one key by the loop turn that started them rather than
            # by which scoring finished first, and it retires whatever the previous
            # attempt left pending. Without it, a scoring that straggled past its own
            # compaction's notice would have its record popped by the NEXT compaction
            # and shown as that one's measurement.
            attempt = begin_attempt(key)
            # The transcript BOUNDARY, read synchronously so it precedes anything the
            # compaction appends. The scoring task reads the transcript itself, and by
            # the time it does, ``provider.compact()`` may already have added rows -- the
            # summary it produced, a recycle notice -- which would then be scored as if
            # they had been there before the compaction they describe. A clock read is
            # free; snapshotting the ROWS here would be a megabyte file read on the event
            # loop, which is the one thing this hook may not do.
            before = datetime.now(timezone.utc).isoformat()
            task = asyncio.create_task(score_compaction(key, attempt, before=before))
            # Tracked on the owner's set, the same place ``_trigger_compaction`` parks
            # its own background task: an untracked task can be garbage-collected
            # mid-await, which turns a shadow measurement into a warning nobody can
            # act on.
            self._owner._background_tasks.add(task)
            task.add_done_callback(self._owner._background_tasks.discard)
        except Exception:
            self._deps.logger.debug(
                "Session %s compaction left unmeasured by the decision seam", key, exc_info=True
            )

    async def _compact_session(self, key: str, pct: float) -> str:
        """Run the backend-specific compaction: Claude in place, everything else through the ladder."""
        owner = self._owner
        # Before the first branch, so the observation covers every backend; not
        # awaited, so it cannot sit between the trigger and the compaction.
        self._shadow_score_compaction(key)
        try:
            session = owner._sessions.get(key)
            if session and self._deps.is_claude_backend(session.provider):
                # Claude keeps the same native session across its compact
                # boundary, so the durable mapping remains untouched.
                claude_session = session

                async def _run_compact() -> None:
                    async with claude_session.semaphore:
                        await claude_session.provider.compact()

                timeout = self._deps.compact_wait_timeout_secs()
                try:
                    # One budget covers both waiting for a live turn and the
                    # compact call itself.
                    await asyncio.wait_for(_run_compact(), timeout=timeout)
                except (Exception, asyncio.TimeoutError) as exc:
                    if isinstance(exc, asyncio.TimeoutError):
                        self._deps.logger.error(
                            "Compact timed out after %.0fs for %s", timeout, key
                        )
                    else:
                        self._deps.logger.exception("Compact failed for %s", key)
                    self.state.cooldown_until[key] = (
                        time.monotonic() + self._deps.compact_failure_cooldown_secs
                    )
                    await owner._fire_compact_callback(key, pct, success=False)
                    return "failed"

                # The guarded reset must happen before the callback, whose
                # arbitrary surface I/O could otherwise let a queued turn
                # complete and then be erased by this verdict.
                did_reset = False
                if owner._settle_compact_cooldown(key, claude_session.provider, pct):
                    did_reset = await owner._reset_still_critical(
                        key,
                        pct,
                        claude_session.provider.context_usage_pct(),
                        expect=claude_session,
                    )
                self._deps.logger.info("Compacted session %s (context overflow)", key)
                await owner._fire_compact_callback(key, pct, success=True)
                return "reset" if did_reset else "ok"

            if session is None:
                return "absent"
            outcome = await self._run_method_ladder(key, session, pct)
            if outcome == "busy":
                # A held turn is a deferral, not a failure: no recycle,
                # cooldown, or failure callback is allowed here.
                self._deps.logger.warning(
                    "Session %s compaction deferred — turn still active after %.0fs",
                    key,
                    self._deps.compact_wait_timeout_secs(),
                )
            return outcome
        except Exception:
            self._deps.logger.exception("Session compaction/recycle failed for %s", key)
            return "failed"
        finally:
            self.state.compacting.discard(key)

    async def _run_method_ladder(self, key: str, session: Any, pct: float) -> str:
        """Walk down from ``session.compaction_method``; the first method that runs wins.

        The key is a ceiling, not an order: the methods form one line of
        strength (``shake`` carries a digest and the tail, ``soft`` the tail,
        ``native`` summarizes in place), and each rotation method degrades one
        way, so the walk is the chosen method followed by every weaker one,
        ``native`` last. ``native`` is the in-place ``/compact`` (or, on a
        backend with no ``/compact`` to send, the unmanaged recycle) and ends
        the walk whatever it returns, because its own failure path already
        recycles. A rotation method answers ``unavailable`` or ``insufficient``
        to hand the turn to the next, with one exception: ``shake`` answering
        ``unavailable`` (no seed writer here, so no digest can be written) hands
        straight to ``native``, because ``soft`` would drop the pre-tail history
        the operator asked to keep. The default, ``native``, is this branch's
        pre-ladder dispatch exactly. Every attempted method logs one INFO line.
        A key whose last rotation was judged ineffective by its successor's
        first reading (``rotation_hold``) runs ``native`` outright while the
        configured method, the window and the threshold are the ones that
        rotation's projection read; a change to any of them walks the ladder again.
        """
        owner = self._owner
        hold = self.state.rotation_hold.get(key)
        if hold is not None and hold == self._rotation_identity(key, session.provider):
            self._deps.logger.info(
                "Session %s compaction: last rotation judged ineffective, running native", key
            )
            ladder: tuple[str, ...] = ()
        else:
            if hold is not None:
                # The projection that judged the held rotation read other inputs;
                # the new ones get their own verdict.
                del self.state.rotation_hold[key]
            # Normalized at load to a known name; the ladder below it is fixed.
            ladder = rotation_ladder(owner._cfg.session.compaction_method)
        for method in ladder:
            try:
                outcome = await self._rotate(method, key, session, pct)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A rotation that raises (an unreadable transcript under the
                # seed writer, a provider without a window figure) is one more
                # way of stepping aside: a method that cannot run never ends the
                # walk, so it reaches native.
                self._deps.logger.warning(
                    "Session %s compaction method %s raised; trying the next method",
                    key,
                    method,
                    exc_info=True,
                )
                outcome = OUTCOME_UNAVAILABLE
            self._deps.logger.info("Session %s compaction method %s: %s", key, method, outcome)
            if outcome not in (OUTCOME_UNAVAILABLE, OUTCOME_INSUFFICIENT):
                return outcome
            if method == COMPACTION_METHOD_SHAKE and outcome == OUTCOME_UNAVAILABLE:
                # Choosing shake over soft is choosing the digest over speed. When
                # no digest can be written here (no seed writer, no window figure,
                # a raising writer), soft would drop the pre-tail history undigested
                # on exactly the surfaces whose users cannot see the loss, so the
                # walk skips it: native still keeps that history as a summary.
                # insufficient is different: soft's projection is smaller by the
                # digest's share, so it can fit where shake did not.
                break
        if _compaction_unmanaged_backend(session.provider) is not None:
            # No ``/compact`` to send and no harness-side summarization to
            # wait for, so there is nothing for ``_compact_in_place`` to do
            # except spend its whole timeout proving it: the prompt would
            # land as ordinary text, no compaction status would follow, and
            # ``wait_for_compaction`` would strand until the deadline before
            # recycling anyway. Recycle straight away instead -- same
            # destination, none of the wait, and the reason is stated rather
            # than inferred from a timeout.
            return await owner._recycle_unmanaged(key, session, pct)
        outcome = await owner._compact_in_place(key, session, pct)
        self._deps.logger.info(
            "Session %s compaction method %s: %s", key, COMPACTION_METHOD_NATIVE, outcome
        )
        return outcome

    async def _rotate(self, method: str, key: str, session: Any, pct: float) -> str:
        """Run one rotation method: recycle the session so its successor re-seeds from the transcript.

        Returns ``unavailable`` / ``insufficient`` to advance the ladder, else
        the terminal result of the walk: ``recycled``, or ``busy`` when a turn
        still holds the semaphore (every method needs it, so nothing later in
        the order could run either).
        """
        window = session.provider.context_window_tokens()
        if window <= 0:
            # No window figure: nothing to project from, and the successor's
            # replay would size its tail from the reference window blind.
            return OUTCOME_UNAVAILABLE
        writer = self.state.seed_writer
        if method == COMPACTION_METHOD_SHAKE:
            if writer is None or not writer.supports(key):
                # No digest can be written on this surface whatever the window
                # (no writer at all, or the one writer holds no transcript for
                # this key: a channel-born session, a key with no tab). Answered
                # before projecting, so the ladder hears unavailable (and hands
                # to native) rather than insufficient (and soft): an insufficient
                # projection here would otherwise slide to soft with the writer
                # never asked, and soft drops the pre-tail history undigested.
                return OUTCOME_UNAVAILABLE
        elif method != COMPACTION_METHOD_SOFT:
            return OUTCOME_UNAVAILABLE
        # The tail the successor carries is the replay's own budget for this
        # window (one figure, shared with the seed writer and the replay itself);
        # shake's digest rides beside it at its share of the same budget.
        tail_tokens = estimate_tokens(replay_budget_chars(window))
        carried = tail_tokens
        if method == COMPACTION_METHOD_SHAKE:
            carried += tail_tokens // SEED_BUDGET_DIVISOR
        projected = self._project_rotation(window, carried)
        if not method_sufficient(
            projected,
            threshold_pct=self.effective_autocompact_pct(key),
            min_effect_pct_points=self._deps.compact_min_effect_pct_points,
        ):
            return OUTCOME_INSUFFICIENT

        timeout = self._deps.compact_wait_timeout_secs()
        try:
            await asyncio.wait_for(session.semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return "busy"
        try:
            # The key can change owners while this rotation waits; checking under
            # the registry lock keeps stale work from seeding or recycling its successor.
            async with self._owner._lock:
                if self._owner._sessions.get(key) is not session:
                    return "absent"
            all_kept = False
            if method == COMPACTION_METHOD_SHAKE and writer is not None:
                written = await writer(key, method, window)
                if written in (SEED_NOTHING, SEED_TAIL_ONLY):
                    # Both named no-seed answers recycle as soft under this hold,
                    # so a queued turn cannot move the tail boundary first. Only
                    # ``SEED_NOTHING`` vouches that replay admission and the tail
                    # split dropped no row; ``SEED_TAIL_ONLY`` keeps soft's loss
                    # notice. The method says what runs in either case.
                    method = COMPACTION_METHOD_SOFT
                    all_kept = written == SEED_NOTHING
                elif written != SEED_WRITTEN:
                    # The surface went away under the rotation (a closed tab),
                    # or the writer answered something this contract does not
                    # name. Either way no digest exists, and the fail-safe
                    # reading is the one that keeps the history: native.
                    return OUTCOME_UNAVAILABLE
            self.state.rotating[key] = method
            if all_kept:
                self.state.rotation_kept_all.add(key)
            try:
                # Same owner-facade hop as the in-place failure path, so the
                # exact-identity recycling marker and its monkeypatches hold.
                await self._owner._recycle_held(key, session, pct)
            finally:
                self.state.rotating.pop(key, None)
                self.state.rotation_kept_all.discard(key)
            # The projection judged this rotation BEFORE it ran; the successor
            # does not exist yet, so its real starting usage is read on its
            # first confirmed turn, through the same deferred verdict the
            # native path uses. That reading arms the failure cooldown when the
            # rotation freed too little, and ``pending_rotation`` lets the
            # settle hold the next walk to native (see ``_run_method_ladder``).
            self.state.pending_verdict[key] = pct
            self.state.pending_rotation[key] = method
        finally:
            session.semaphore.release()
        return "recycled"

    def _project_rotation(self, window: int, carried_tokens: int) -> float | None:
        """Usage the successor would start at; ``None`` only for a window of 0, which the caller rules out."""
        return project_pct_after(
            window_tokens=window,
            carried_tokens=carried_tokens,
            overhead_tokens=estimate_tokens(startup_context_chars(window)),
        )

    def _rotation_identity(self, key: str, provider: Any) -> tuple[str, int, float]:
        """The inputs a rotation's projection and verdict read for *key*: method, window, threshold."""
        return (
            self._owner._cfg.session.compaction_method,
            provider.context_window_tokens(),
            self.effective_autocompact_pct(key),
        )

    async def _recycle_held(
        self, key: str, session: Any, pct: float, *, uncompactable: bool = False
    ) -> None:
        """Recycle an exact session while its turn semaphore remains held.

        The caller owns the semaphore.  Pop-by-identity keeps a racing fresh
        registration alive, while clearing only the resume SID preserves the
        channel/linkage data attached to the mapping.

        ``uncompactable`` says WHY, and only the caller knows: this method is reached
        both from a compaction that FAILED on a backend that can compact, and from a
        backend that cannot compact at all.  The two look identical from here, and a
        surface that received one label for both told a kiro-cli user their backend
        has no compaction.  Threaded to the callback rather than inferred.
        """
        owner = self._owner
        owner._recycling[key] = session
        if uncompactable:
            self.state.uncompactable_recycles.add(key)
        try:
            async with owner._lock:
                popped = None
                if owner._sessions.get(key) is session:
                    popped = owner._sessions.pop(key, None)
                    owner._advance_session_generation(key)
                    # Same tick as the pop. Only this branch records: on the
                    # other one the registry already holds a SUCCESSOR under
                    # this key, whose start must stay its own.
                    await record_session_ended(key, end_reason=END_REASON_RECYCLED)

            await asyncio.to_thread(self._deps.unlink_session_queue, session)
            if popped is None:
                await session.provider.shutdown()
                self._deps.logger.info(
                    "Recycled session %s (context overflow; entry already replaced)", key
                )
            else:
                owner._session_map.clear_sid(key)
                await popped.provider.shutdown()
                self._deps.logger.info("Recycled session %s (context overflow; sid cleared)", key)
            await owner._fire_compact_callback(key, pct, success=True)
        finally:
            if owner._recycling.get(key) is session:
                owner._recycling.pop(key, None)
                # Paired with the ``add`` above, under the same identity guard as
                # the pop: the marker's life is exactly this recycle.  The
                # callback consumes it on the way out, so this discard only ever
                # matters when a teardown step raised BEFORE the callback ran --
                # and without it the stale marker would label the next recycle on
                # this key an uncompactable backend, which on a kiro-cli session
                # claims a missing capability the harness has.
                self.state.uncompactable_recycles.discard(key)

    async def _recycle_unmanaged(self, key: str, session: Any, pct: float) -> str:
        """Recycle a session no compaction path can reach, turn-exclusive.

        The threshold answer for a member of ``ACP_BACKENDS_CONTEXT_RECYCLE``, which
        is the only population that reaches this method -- a backend in none of the
        three compaction sets is DECLINED at the gate and never arrives here.  Its
        context grows until
        the harness's own window ends the conversation for it, so bounding the
        context is the only outcome available -- and it is the SAME outcome
        ``_compact_in_place`` already reaches when a compaction fails, taken
        without first spending the timeout that proves the compaction cannot
        happen.

        Turn exclusion is acquired the way ``_compact_in_place`` acquires it,
        and for the identical reason: ``_recycle_held`` shuts the provider down,
        and a queued turn that entered meanwhile would be talking to a process
        that is going away.  A held turn answers ``busy`` -- a deferral, not a
        failure, so no cooldown and no failure callback, matching the ``busy``
        contract ``_compact_session`` already documents.
        """
        timeout = self._deps.compact_wait_timeout_secs()
        try:
            await asyncio.wait_for(session.semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return "busy"
        try:
            await self._owner._recycle_held(key, session, pct, uncompactable=True)
        finally:
            session.semaphore.release()
        return "recycled"

    async def _compact_in_place(self, key: str, session: Any, pct: float) -> str:
        """Run native ``/compact`` while excluding turns on this session.

        Failure recycling intentionally happens before releasing the
        semaphore.  A release/reacquire gap lets a queued turn enter a client
        that is still compacting, consume the late completion event, and hang
        without an end-turn boundary.
        """
        timeout = self._deps.compact_wait_timeout_secs()
        try:
            await asyncio.wait_for(session.semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return "busy"

        started = time.monotonic()
        result_wait_used: float | None = None
        try:

            async def _run() -> None:
                nonlocal result_wait_used
                # Keep this lazy: importing the ACP package eagerly recreates
                # the providers/session cycle avoided by this module boundary.
                from kiro_crew.acp.types import EVENT_COMPACTION_STATUS

                status: str | None = None
                async for event in session.provider.stream_command("/compact"):
                    if event.kind == EVENT_COMPACTION_STATUS and event.text in (
                        "completed",
                        "failed",
                    ):
                        status = event.text
                if status is None:
                    # No special case for an inline harness HERE. This loop
                    # drives the harness through ``stream_command`` rather than
                    # ``provider.compact()``, and the wait below is the method
                    # that answers immediately for a member of
                    # ``ACP_BACKENDS_INLINE_COMPACTION`` -- so both routes to a
                    # compaction settle in ONE place. Teaching this one call
                    # site instead would have left the same strand at every
                    # other site that awaits a compaction.
                    result_wait_used = self._deps.compact_result_wait_secs(
                        time.monotonic() - started
                    )
                    result = await session.provider.wait_for_compaction(timeout=result_wait_used)
                    status = result.get("type") if isinstance(result, dict) else None
                if status != "completed":
                    raise RuntimeError(f"compaction reported {status or 'no result'}")

            # The inner status wait spends the full remainder; margin keeps its
            # graceful no-result diagnostic ahead of the outer backstop.
            await asyncio.wait_for(
                _run(), timeout=timeout + self._deps.compact_result_wait_margin_secs
            )
        except (Exception, asyncio.TimeoutError):
            self._deps.logger.warning(
                "Session %s in-place /compact failed after %.0fs — recycling "
                "(semaphore held; async status wait %s)",
                key,
                time.monotonic() - started,
                "never reached" if result_wait_used is None else f"{result_wait_used:.0f}s",
                exc_info=True,
            )
            # This owner-facade hop is load-bearing for both monkeypatches and
            # the lifecycle boundary's exact-identity recycling marker.
            await self._owner._recycle_held(key, session, pct)
            return "recycled"
        finally:
            session.semaphore.release()

        escalate = self._owner._settle_compact_cooldown(key, session.provider, pct)
        self._deps.logger.info("Compacted session %s in place (context overflow)", key)
        did_reset = False
        if escalate:
            did_reset = await self._owner._reset_still_critical(
                key, pct, session.provider.context_usage_pct(), expect=session
            )
        await self._owner._fire_compact_callback(key, pct, success=True)
        return "reset" if did_reset else "ok"

    def _settle_compact_cooldown(self, key: str, provider: LLMProvider, pct_before: float) -> bool:
        """Settle immediately when trustworthy, otherwise defer one verdict."""
        pct_after = provider.context_usage_pct()
        unknown = self._deps.context_pct_is_unknown(provider)
        if unknown or pct_after >= pct_before:
            self.state.pending_verdict[key] = pct_before
            self._deps.logger.info(
                "Session %s compaction effect not measurable yet "
                "(%.1f%% -> %.1f%%%s) — verdict deferred to the next confirmed reading",
                key,
                pct_before,
                pct_after,
                ", unconfirmed" if unknown else "",
            )
            return False
        self.state.pending_verdict.pop(key, None)
        # Append-only the session's log (flag-gated, fail-soft). This is the
        # immediately-confirmed half; a deferred verdict is recorded by
        # ``_compaction_gate_decision`` when its reading settles, so every
        # compaction reaches the crew log on exactly one of the two paths.
        # Deferred for the boot-path rule; see the note at the other call site.
        from kiro_crew.crew_log import emit as crew_log_emit

        crew_log_emit.on_compaction_applied(
            crew_log_emit.session_id_of(provider),
            pct_before=pct_before,
            pct_after=pct_after,
        )
        return self._owner._judge_compact_effect(key, pct_before, pct_after)

    def _floor_pct(self, key: str) -> float | None:
        """The recorded no-conversation floor of *key*'s live session, if known."""
        session = self._owner._sessions.get(key)
        floor = getattr(session, "floor_pct", None)
        return floor if isinstance(floor, (int, float)) else None

    def _judge_compact_effect(self, key: str, pct_before: float, pct_after: float) -> bool:
        """Arm damping for an ineffective drop and report critical context."""
        freed = pct_before - pct_after
        if freed < self._deps.compact_min_effect_pct_points:
            self.state.cooldown_until[key] = (
                time.monotonic() + self._deps.compact_failure_cooldown_secs
            )
            still_critical = pct_after >= self._deps.post_compact_reset_pct
            verdict = (
                "still critical — escalating to reset" if still_critical else "cooldown applied"
            )
            if still_critical:
                floor = self._floor_pct(key)
                if (
                    floor is not None
                    and pct_after - floor < self._deps.compact_min_effect_pct_points
                ):
                    # A reset replaces the conversation with NOTHING and keeps
                    # everything else, so the most it can free is the distance
                    # down to the floor. When that distance is under the same
                    # bar compaction just failed, the reset would destroy the
                    # conversation and land on a reading that is critical again.
                    # The message reports the two numbers it compared and
                    # nothing it did not measure: the floor is a reading, not a
                    # breakdown of what fills the window.
                    still_critical = False
                    verdict = (
                        f"still critical, but a fresh session on this key already read "
                        f"{floor:.1f}% at its first confirmed reading — a reset is "
                        f"unlikely to free {self._deps.compact_min_effect_pct_points:.1f} "
                        f"points; keeping the conversation and the cooldown"
                    )
            self._deps.logger.warning(
                "Session %s compaction ineffective — context %.1f%% -> %.1f%% "
                "(freed %.1f < %.1f points); %s",
                key,
                pct_before,
                pct_after,
                freed,
                self._deps.compact_min_effect_pct_points,
                verdict,
            )
            return still_critical
        self.state.cooldown_until.pop(key, None)
        return False

    async def _reset_still_critical(
        self, key: str, pct_before: float, pct_after: float, *, expect: Any | None
    ) -> bool:
        """Guardedly reset the exact idle session measured as still critical."""
        owner = self._owner
        self._deps.logger.warning(
            "Session %s still at %.0f%% after compaction (was %.0f%%) — resetting",
            key,
            pct_after,
            pct_before,
        )
        try:
            did_reset = await owner.reset(
                key,
                expect_session=expect,
                skip_if_busy=True,
                clear_conversation=True,
                # ``clear_conversation`` IS the conversation ending, so this reset is an
                # end and not the recycle the rest of this module performs. It clears the
                # native resume sid and suppresses replay, so the successor cold-starts
                # with none of this conversation's history -- there is nothing for a child
                # to deliver into, and a child that reports anyway resolves the parent
                # through ``get_or_create`` and re-opens the conversation this call threw
                # away, seeded with a retired run's output.
                #
                # The other reset in this module is the ordinary auto-compaction retry and
                # keeps the default: same conversation, new process.
                ends_conversation=True,
            )
        except Exception:
            self._deps.logger.exception("Session %s critical reset failed", key)
            return False
        if not did_reset:
            self._deps.logger.info(
                "Session %s critical reset skipped — %s; the next threshold "
                "crossing re-attempts after the cooldown",
                key,
                (
                    "session replaced since the verdict"
                    if expect is not None and owner._sessions.get(key) is not expect
                    else "turn in flight"
                ),
            )
        return did_reset

    async def _fire_compact_callback(self, key: str, pct: float, *, success: bool) -> None:
        """Mark reinjection and invoke the compact callback, swallowing errors."""
        # Every compaction that reached a verdict passes here, whether or not a
        # callback is registered, so this is where the counter belongs: the early
        # return below would otherwise drop the surfaces that register none.
        #
        # The counter's success is NOT the callback's success. ``_recycle_held``
        # fires this with success=True because the SESSION now has headroom, which
        # is what the callback needs to know -- but it is reached exactly when an
        # in-place /compact FAILED and the provider had to be replaced instead.
        # Counting that as a successful compaction would report the failure mode
        # as the success case. The recycling marker is set for the whole of
        # ``_recycle_held`` and popped only after this call, so it is what
        # separates the two populations here; ``test_a_failed_compact_that_recycles
        # _is_not_counted_successful`` pins that ordering.
        #
        # A rotation method (``soft``, ``shake``) reaches this funnel through the
        # SAME recycle primitive, on purpose: for it the recycle IS the
        # compaction, so ``rotating`` (set for the same span as the marker)
        # restores the success the marker would otherwise demote.
        recycled = key in self._owner._recycling
        intentional = key in self.state.rotating
        # The method tag separates rotations from native summaries, so a
        # rotation recycling every turn shows up as its own series rather than
        # hiding inside the success bit. The callback gets the same name: the
        # notice it posts is the only place the user learns that the pre-tail
        # history was dropped or digested rather than summarized in place.
        method = self.state.rotating.get(key, COMPACTION_METHOD_NATIVE)
        emit_counter(
            CONTEXT_COMPACTIONS,
            {
                "success": bool(success) and (not recycled or intentional),
                "method": method,
            },
        )
        # The same marker that keeps a recycle out of the success counter also tells
        # the callback WHICH outcome it is reporting. Without it a recycle arrives
        # indistinguishable from a compaction, and the dashboard announced one as the
        # other -- "Auto-compacted at N%." on a session whose history had just been
        # discarded, which is the untruth this whole change set out to remove. A
        # rotation is a recycle here too; ``method`` is what tells the surface it
        # was the intended kind.
        if not recycled:
            outcome = COMPACT_OUTCOME_COMPACTED
        elif key in self.state.uncompactable_recycles:
            # Consumed HERE, at the end of the recycle that set it: the marker's whole
            # life is one recycle, and leaving it behind would mislabel the NEXT one on
            # this key -- which on a kiro-cli session would claim a missing capability
            # the harness has.
            self.state.uncompactable_recycles.discard(key)
            outcome = COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE
        else:
            outcome = COMPACT_OUTCOME_RECYCLED
        # Recycling destroys this session, so its successor receives startup
        # context normally.  The identity guard also avoids flagging a racing
        # replacement while the old provider is being reaped.
        if success and key not in self._owner._recycling:
            self._owner.mark_needs_reinjection(key)
        callback = self.state.on_compacted
        if callback is None:
            return
        # Read for the callback alone, after the counter: a soft rotation that
        # dropped nothing is still ``soft`` to the counter's series and to the
        # verdict, and only the notice needs to know the tail carried every row.
        all_kept = key in self.state.rotation_kept_all
        try:
            await callback(
                key, pct, success=success, outcome=outcome, method=method, all_kept=all_kept
            )
        except Exception:
            self._deps.logger.exception("Compact callback failed for %s", key)
