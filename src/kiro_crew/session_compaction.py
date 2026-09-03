"""Context-compaction policy and execution for :mod:`kiro_crew.session`.

The coordinator deliberately depends on a small owner facade instead of
importing ``kiro_crew.session``.  Session allocation and teardown still own the
registry, its lock, and the exact-identity ``_recycling`` marker; compaction
borrows those seams without becoming a second session manager.

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
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew.metrics.events import CONTEXT_COMPACTIONS, emit_counter
from kiro_crew.metrics.sessions import END_REASON_RECYCLED, record_session_ended

if TYPE_CHECKING:
    # Type-only: importing providers.base from this leaf at runtime enters the
    # providers -> acp package -> runtime -> session_pid -> providers cycle.
    from kiro_crew.providers.base import LLMProvider
else:
    LLMProvider = Any


class CompactCallback(Protocol):
    async def __call__(self, key: str, pct: float, *, success: bool) -> None: ...  # noqa: E704


@dataclass(slots=True)
class CompactionState:
    """Mutable state owned exclusively by the compaction boundary."""

    compacting: set[str] = field(default_factory=set)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    pending_verdict: dict[str, float] = field(default_factory=dict)
    #: Per-session threshold overrides (folded key -> pct). A key absent here
    #: falls back to the published global (``cfg.session.autocompact_pct``).
    #: Deliberately independent of ``_sessions`` membership: an override is a
    #: user preference on the conversation, so it survives session resets and
    #: recycles, and is re-seeded from slot persistence after a restart.
    pct_overrides: dict[str, float] = field(default_factory=dict)
    on_compacted: CompactCallback | None = None


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

    def _trigger_compaction(
        self, key: str, reason: str, pct: float, provider: LLMProvider
    ) -> str | None: ...

    def _compaction_gate_decision(
        self, key: str, provider: LLMProvider, pct: float
    ) -> str | None: ...

    async def _compact_session(self, key: str, pct: float) -> str: ...

    async def _compact_in_place(self, key: str, session: Any, pct: float) -> str: ...

    async def _recycle_held(self, key: str, session: Any, pct: float) -> None: ...

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
    ) -> bool: ...


def _compact_unsupported_backend(provider: LLMProvider) -> str | None:
    """Backend id this provider names as unable to serve ``/compact``, else None.

    The same capability #7800 gave the manual entry points, read for the
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

    def drop_autocompact_overrides_matching(
        self, exact_keys: set[str], folded_keys: set[str], fold: Callable[[str], str]
    ) -> int:
        """Drop overrides for permanently deleted sessions with NO live session.

        ``destroy()`` clears a live session's override, but a permanent delete
        of ARCHIVED history has no session to destroy — and channel keys are
        deterministic, so a recreated session would silently inherit the
        deleted conversation's threshold. Same fold-matching contract as the
        session-ledger purge sweep: an override matches when its stored key is
        in ``exact_keys`` or its ``fold``-ed spelling is in ``folded_keys``.
        Returns the number of entries dropped.
        """
        doomed = [k for k in self.state.pct_overrides if k in exact_keys or fold(k) in folded_keys]
        for k in doomed:
            self.state.pct_overrides.pop(k, None)
        return len(doomed)

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
        behaviour that changes for such a backend is the one that was broken --
        the dispatch itself -- and it changes before ``_compact_session`` is
        ever scheduled, so no ``compacting`` entry, no background task and no
        ``session.semaphore`` acquisition happens for a compaction that could
        only have ended in the 300s strand (#7812).
        """
        baseline = self.state.pending_verdict.get(key)
        if baseline is not None and not self._deps.context_pct_is_unknown(provider):
            del self.state.pending_verdict[key]
            # Ignore the escalation result here: a deferred reading includes a
            # later turn's growth and is only safe for cooldown damping.
            self._owner._judge_compact_effect(key, baseline, pct)

        if self._deps.is_cc_managed(provider):
            return "cc_managed"
        if pct < self.effective_autocompact_pct(key):
            return "below_threshold"
        unsupported = _compact_unsupported_backend(provider)
        if unsupported is not None:
            # Declining, not recycling. The one current non-member (KAS)
            # summarizes on its own initiative and its
            # ``summarization_completed`` frame calls
            # ``reset_after_compaction()`` on the meter
            # (``acp/session_handle.py``), so the reading this gate just read
            # falls back below the threshold without us acting -- the same
            # relationship ``cc_managed`` encodes for Claude-Code sessions.
            # Recycling sooner would not have been the smaller harm: it
            # destroys the live conversation, which is the second half of the
            # reported defect and not merely its consequence.
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
            self._deps.logger.info(
                "Session %s context at %.0f%% -- %s manages compaction itself; "
                "skipping the /compact dispatch it cannot answer",
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

    async def _compact_session(self, key: str, pct: float) -> str:
        """Run the backend-specific in-place compaction policy."""
        owner = self._owner
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
            outcome = await owner._compact_in_place(key, session, pct)
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

    async def _recycle_held(self, key: str, session: Any, pct: float) -> None:
        """Recycle an exact session while its turn semaphore remains held.

        The caller owns the semaphore.  Pop-by-identity keeps a racing fresh
        registration alive, while clearing only the resume SID preserves the
        channel/linkage data attached to the mapping.
        """
        owner = self._owner
        owner._recycling[key] = session
        try:
            async with owner._lock:
                popped = None
                if owner._sessions.get(key) is session:
                    popped = owner._sessions.pop(key, None)
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
        return self._owner._judge_compact_effect(key, pct_before, pct_after)

    def _judge_compact_effect(self, key: str, pct_before: float, pct_after: float) -> bool:
        """Arm damping for an ineffective drop and report critical context."""
        freed = pct_before - pct_after
        if freed < self._deps.compact_min_effect_pct_points:
            self.state.cooldown_until[key] = (
                time.monotonic() + self._deps.compact_failure_cooldown_secs
            )
            still_critical = pct_after >= self._deps.post_compact_reset_pct
            self._deps.logger.warning(
                "Session %s compaction ineffective — context %.1f%% -> %.1f%% "
                "(freed %.1f < %.1f points); %s",
                key,
                pct_before,
                pct_after,
                freed,
                self._deps.compact_min_effect_pct_points,
                ("still critical — escalating to reset" if still_critical else "cooldown applied"),
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
        recycled = key in self._owner._recycling
        emit_counter(CONTEXT_COMPACTIONS, {"success": bool(success) and not recycled})
        # Recycling destroys this session, so its successor receives startup
        # context normally.  The identity guard also avoids flagging a racing
        # replacement while the old provider is being reaped.
        if success and key not in self._owner._recycling:
            self._owner.mark_needs_reinjection(key)
        callback = self.state.on_compacted
        if callback is None:
            return
        try:
            await callback(key, pct, success=success)
        except Exception:
            self._deps.logger.exception("Compact callback failed for %s", key)
