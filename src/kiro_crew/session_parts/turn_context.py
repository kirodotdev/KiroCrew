"""What a session's next turn carries: its context-window budget and the history it owes.

Context readings, the compaction thresholds and gate, the compaction calls, the
one-shot reinjection marker, and the replay a provider switch, a fresh start or a
suppressed cold start leaves owed.

At >= ``cfg.session.autocompact_pct`` context usage, fires a background
compaction task. A backend that can serve ``/compact`` compacts **in
place** so the session — and any queued or agentic work on it —
continues without a user nudge; one that cannot is declined before any
dispatch:

* **kiro-cli:** run ``/compact`` in place under the session semaphore
  (native command execute + ``_kiro.dev/compaction/status`` wait). The
  process and session ID survive; the conversation is summarized in
  place. If the in-place compact fails or times out, fall back to the
  legacy **recycle**: kill the session and let the next user message
  re-seed context via ``build_session_context()`` (the stale resume SID is
  cleared while the session-map entry and channel linkage remain). A recycle is never
  forced through a live turn — if the turn semaphore cannot be acquired,
  the attempt is skipped and re-triggered at the next turn end.
* **claude-agent-acp:** run ``/compact`` in place under the session
  semaphore. The SDK preserves the same session ID across the
  compact_boundary; the session keeps its summary and continues without
  a recycle.
* **KAS:** nothing is dispatched. KAS treats the ``/compact`` prompt as
  ordinary text and never answers it with a compaction status, so the
  gate declines (``compact_unsupported``) before the compaction task is
  scheduled: an ungated dispatch strands the wait for the full budget
  while holding the semaphore and then recycles the session. KAS
  summarizes on its own initiative, the same way ``cc_managed`` leaves
  Claude-Code sessions to compact themselves.

A failed compact records a per-key cooldown so a broken /compact does
not fire on every subsequent turn. The compact callback fires on both
success and failure; the dashboard uses ``success`` to choose the
banner copy. The user's response is never blocked — compaction is
fire-and-forget.

Each function here whose first parameter is ``self`` is a ``SessionManager``
member: the class binds it by name (``kiro_crew.session``), so it reads and writes the
manager's own state on the instance, and a patch of the member on the class or
on an instance reaches every caller.

Instance fields written: ``_effort_intent_writes``.

Instance fields read: ``_cfg``, ``_compaction``, ``_session_map``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING

from kiro_crew.config.loader import AUTOCOMPACT_PCT_MAX, AUTOCOMPACT_PCT_MIN
from kiro_crew.messaging.link import canonical_key
from kiro_crew.session_map import (
    EXPLICIT_EFFORT_DEFAULT_FLAG,
    SUPPRESS_REPLAY_FLAG,
    EffortDefaultRowRefused,
)
from kiro_crew.session_parts.policy import _resolve_compact_wait_secs
from kiro_crew.session_parts.record import _EffortIntentWrites

if TYPE_CHECKING:
    from kiro_crew.session import LLMProvider, SessionManager
    from kiro_crew.session_parts.policy import _CompactCallback
    from kiro_crew.session_parts.record import _Session

logger = logging.getLogger("kiro_crew.session")


def compact_wait_budget_secs(self: SessionManager) -> float:
    """The compaction wait budget this manager's config is in force with.

    The ONE resolver for ``session.compact_wait_secs``: the automatic
    coordinator, the task runner's context-overflow compaction, the
    dashboard ``/compact`` and every chat channel's compact command and
    near-limit compaction all hold this manager and read the budget here,
    so no caller can resolve the key differently. The manager's config is
    the one the process booted with, re-adopted on every live change, so a
    change applies to the next compaction, and a standalone
    ``kirocrew run`` (no live-config watcher) still honours the key. Read
    per call -- a plain attribute read, safe on the event loop.
    """
    return _resolve_compact_wait_secs(self._cfg.session.compact_wait_secs)


def check_context_usage(
    self: SessionManager, key: str, provider: LLMProvider, *, defer_compaction: bool = False
) -> float:
    """Delegate accounting; a human handoff may defer non-critical compaction."""
    return self._compaction.check_context_usage(key, provider, defer_compaction=defer_compaction)


def effective_autocompact_pct(self: SessionManager, key: str) -> float:
    """Delegate *key*'s live compaction threshold.

    Precedence, highest first: the session's own override, the default its
    agent declares (``agents.<name>.autocompact_pct``), the global.

    The READ half of :meth:`set_autocompact_pct`, for a caller that must know
    what a context reading fires at before it changes that reading's window.

    Adopts a newly published threshold first, for the reason
    :meth:`_compaction_gate_decision` does: that ladder reads the threshold at the
    END of a turn while this answers a caller deciding BEFORE it, so a read that
    skipped the sync would measure one turn against two different numbers.
    """
    self._sync_autocompact_pct()
    return self._compaction.effective_autocompact_pct(key)


def compaction_due_after_turn(self: SessionManager, key: str, provider: LLMProvider) -> bool:
    """Delegate: would the end-of-turn check compact *key* if the turn ended now?

    Read-only, for a caller inside a running turn. Adopts a newly published
    threshold first, as :meth:`effective_autocompact_pct` does, so the answer is
    measured against the number the turn-end check will use.
    """
    self._sync_autocompact_pct()
    return self._compaction.compaction_due_after_turn(key, provider)


def agent_autocompact_default(self: SessionManager, key: str) -> tuple[str, float] | None:
    """The crew behind *key*'s live session and the threshold it declares.

    What a session with no override of its own follows instead of the
    global, for a surface that names which default is in force. ``None``
    when that crew declares none, or when no live session holds *key*.
    """
    pct = self._compaction.agent_autocompact_pct(key)
    if pct is None:
        return None
    session = self._sessions.get(self._fold_key(key))
    return (getattr(session, "capability_member", "") or "", pct)


def set_autocompact_pct(self: SessionManager, key: str, pct: float | None) -> None:
    """Set or clear (``None``) *key*'s per-session compaction threshold.

    Values clamp into the documented ``AUTOCOMPACT_PCT_MIN``–``MAX`` range,
    the same guarantee the config loader gives the global knob: an
    out-of-range override (e.g. from a hand-edited persistence file) must
    degrade to the nearest firing value, never silently disable the
    backstop. The override is keyed independently of live-session
    membership, so it survives resets and recycles; callers that persist it
    (the dashboard slot) re-seed it after a gateway restart.
    """
    if pct is not None:
        if pct != pct:  # NaN: comparisons below would both be False
            return
        try:
            pct = min(max(float(pct), AUTOCOMPACT_PCT_MIN), AUTOCOMPACT_PCT_MAX)
        except OverflowError:
            # An int too large for a float; ignore like NaN.
            return
    self._compaction.set_autocompact_pct(key, pct)


async def compact_if_needed(self: SessionManager, key: str) -> str:
    """Delegate awaited between-turn compaction."""
    return await self._compaction.compact_if_needed(key)


def set_compact_callback(self: SessionManager, cb: _CompactCallback | None) -> None:
    """Register the compaction completion callback."""
    self._compaction.set_compact_callback(cb)


def set_compacting_callback(self: SessionManager, cb: Callable[[str, bool], None] | None) -> None:
    """Register the observer told when a session enters or leaves compaction."""
    self._compaction.set_compacting_callback(cb)


def is_compacting(self: SessionManager, key: str) -> bool:
    """Whether an automatic compaction is in flight on *key* right now."""
    return self._compaction.is_compacting(key)


def compaction_waits_for_turn(self: SessionManager, key: str) -> bool:
    """Whether *key*'s compaction still waits for the turn holding the session."""
    return self._compaction.compaction_waits_for_turn(key)


def mark_needs_reinjection(self: SessionManager, key: str) -> None:
    """Mark a live session for one-shot context reinjection."""
    self._compaction.mark_needs_reinjection(key)


def consume_needs_reinjection(self: SessionManager, key: str) -> bool:
    """Consume a live session's reinjection marker."""
    return self._compaction.consume_needs_reinjection(key)


def provider_switch_replay_pending(self: SessionManager, key: str) -> bool:
    """Return whether a live session still owes conversation replay.

    The first real claimant can be a native non-destructive slash command,
    which deliberately bypasses prompt construction. Reading without clearing
    lets that command finish while preserving replay for the next prompt. A
    confirmed ``/clear`` is the exception and consumes the marker at its
    provider event.
    """
    session = self._sessions.get(self._fold_key(key))
    return bool(session is not None and session.provider_switch_replay)


def mark_provider_switch_replay(self: SessionManager, key: str) -> bool:
    """Re-arm replay after an accepted turn is discarded by cancellation."""
    session = self._sessions.get(self._fold_key(key))
    if session is None:
        return False
    session.provider_switch_replay = True
    return True


def consume_provider_switch_replay(self: SessionManager, key: str) -> bool:
    """Explicitly retire replay after confirmed native history deletion."""
    session = self._sessions.get(self._fold_key(key))
    if session is None or not session.provider_switch_replay:
        return False
    session.provider_switch_replay = False
    return True


def first_turn_history_owed_pending(self: SessionManager, key: str) -> bool:
    """Return whether a live session still owes its FRESH first-turn history.

    Read without clearing, like ``provider_switch_replay_pending``: a slash
    command bypasses history assembly, so reading-and-clearing here would let
    it spend the debt the next ordinary prompt must still pay.
    """
    session = self._sessions.get(self._fold_key(key))
    return bool(session is not None and session.first_turn_history_owed)


def unlanded_marker_pending(self: SessionManager, key: str) -> bool:
    """Whether *key*'s mapped sid carries an unlanded marker that must reach disk.

    Only a backend whose resume :meth:`SessionMap.get` gates on the marker
    ever has one, so a turn on any other backend has nothing to await.
    """
    return self._session_map.sid_unlanded_pending(self._fold_key(key))


def consume_first_turn_history_owed(self: SessionManager, key: str) -> bool:
    """Settle the FRESH first-turn history debt once a non-slash
    context-bearing turn delivers its history (assembled and accepted) or a
    reset suppresses it."""
    folded = self._fold_key(key)
    session = self._sessions.get(folded)
    if session is None or not session.first_turn_history_owed:
        return False
    session.first_turn_history_owed = False
    self._session_map.clear_sid_unlanded(folded)
    return True


def suppress_replay_persistently(self: SessionManager, key: str) -> None:
    """Mark *key*'s next cold start to start empty, durably.

    Written through THIS manager's map rather than a throwaway one, for the
    reason ``SessionMap``'s rule 3 gives: a detached write is reversed by the
    live map's next mutation and takes its unflushed rows with it.
    """
    self._session_map.set_flag(self._fold_key(key), SUPPRESS_REPLAY_FLAG, True)


def set_explicit_effort_default(self: SessionManager, key: str, pending: bool) -> bool:
    """Record whether *key* must replace an unowned effort overlay once."""
    folded = self._fold_key(key)
    target = folded
    if (
        pending
        and folded != key
        and self._session_map.explicit_effort_default_held(key)
        and not self._session_map.explicit_effort_default_held(folded)
    ):
        # Armed again on the spelling whose row the hold kept counted.
        target = key
    try:
        self._session_map.set_flag(target, EXPLICIT_EFFORT_DEFAULT_FLAG, pending)
    except EffortDefaultRowRefused as exc:
        logger.warning(
            "Could not retain explicit effort Default for %s: %s",
            exc.key,
            exc.reason,
        )
        return False
    if not pending and folded != key:
        self._session_map.set_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG, False)
    return True


def effort_basis_locked(self: SessionManager, key: str) -> bool:
    """Whether an allocation is in flight under *key*.

    A cold start reads the one-shot Default once and publishes a session
    built on that read; its reservation is held from before that read until
    the session is registered (and while a caller claims an already-live
    session), so a pick landing meanwhile that would change that basis is
    refused, not recorded. A pick that would leave it as it is has nothing
    to record and passes.
    """
    return self._has_allocation_reservation(key)


def _effort_intent_writes_in_flight(self: SessionManager) -> dict[str, _EffortIntentWrites]:
    # Lazy, so a manager built without ``__init__`` (as tests do) still has
    # the dict on its first write or wait.
    writes = getattr(self, "_effort_intent_writes", None)
    if writes is None:
        writes = {}
        setattr(self, "_effort_intent_writes", writes)
    return writes


@contextmanager
def effort_intent_write(self: SessionManager, key: str) -> Iterator[None]:
    """Count one write of *key*'s effort intent from its in-memory flag through its save.

    The effort handler holds this around :meth:`set_explicit_effort_default`,
    the ``aflush`` that saves it and the put-back a failed or cancelled save
    makes, entered with no await after its :meth:`effort_basis_locked` check.
    The count uses the flag's canonical key, so a pick and cold start using
    different spellings of one Slack thread meet before the start reads its
    basis. It then reads either the saved value or the one the failed save
    put back, never an in-memory value no save will keep. The key's entry is
    removed once its last write has exited, so nothing here outlives a write.
    """
    writes = self._effort_intent_writes_in_flight()
    intent_key = canonical_key(key)
    entry = writes.get(intent_key)
    if entry is None:
        entry = writes[intent_key] = _EffortIntentWrites()
    entry.count += 1
    try:
        yield
    finally:
        entry.count -= 1
        if entry.count == 0:
            if writes.get(intent_key) is entry:
                del writes[intent_key]
            entry.settled.set()


async def wait_for_effort_intent_writes(self: SessionManager, key: str) -> None:
    """Return once no write of *key*'s effort intent is in flight.

    Awaited by a cold start right before its single read of the key's effort
    basis, with the key's allocation reservation already held: a later pick
    that would change the basis is refused by :meth:`effort_basis_locked`,
    and one that would not enters no write, so the count can only fall
    while this waits. The count uses the flag's canonical key, so either
    spelling of one Slack thread waits for the same write. Returns at once
    when no write is in flight. Bounded by the pick's own save, one
    worker-thread write of the session map. A cancellation while waiting
    propagates; nothing was read or written.
    """
    writes = getattr(self, "_effort_intent_writes", None)
    if not writes:
        return
    intent_key = canonical_key(key)
    while (entry := writes.get(intent_key)) is not None:
        await entry.settled.wait()


def hold_explicit_effort_default(self: SessionManager, key: str) -> AbstractContextManager[None]:
    """Keep *key*'s explicit-Default row counted while a pick may clear it.

    A pick holds both spellings read by
    :meth:`explicit_effort_default_pending` until the request ends, so it
    can put the flag back and no other pick can take the row meanwhile.
    """
    return self._session_map.hold_explicit_effort_default(self._fold_key(key), key)


def explicit_effort_default_pending(self: SessionManager, key: str) -> bool:
    """Return whether either spelling of *key* carries the one-shot Default."""
    folded = self._fold_key(key)
    return self._session_map.get_flag(
        folded, EXPLICIT_EFFORT_DEFAULT_FLAG
    ) or self._session_map.get_flag(key, EXPLICIT_EFFORT_DEFAULT_FLAG)


def _compaction_gate_decision(
    self: SessionManager, key: str, provider: LLMProvider, pct: float
) -> str | None:
    """Delegate the ordered compaction gate ladder."""
    # Adopt a newly published threshold before the ladder reads it: the
    # coordinator resolves it through ``owner._cfg``, so syncing here is what
    # makes a config write from any writer bind on the next reading.
    self._sync_autocompact_pct()
    return self._compaction._compaction_gate_decision(key, provider, pct)


def _trigger_compaction(
    self: SessionManager, key: str, reason: str, pct: float, provider: LLMProvider
) -> str | None:
    """Delegate fire-and-forget compaction scheduling."""
    return self._compaction._trigger_compaction(key, reason, pct, provider)


async def _compact_session(self: SessionManager, key: str, pct: float) -> str:
    """Delegate backend-specific compaction execution."""
    return await self._compaction._compact_session(key, pct)


async def _recycle_held(
    self: SessionManager, key: str, session: "_Session", pct: float, *, uncompactable: bool = False
) -> None:
    """Delegate exact-session recycle while its semaphore is held."""
    await self._compaction._recycle_held(key, session, pct, uncompactable=uncompactable)


async def _recycle_unmanaged(
    self: SessionManager, key: str, session: "_Session", pct: float
) -> str:
    """Delegate the recycle for a backend no compaction path can reach."""
    return await self._compaction._recycle_unmanaged(key, session, pct)


async def _compact_in_place(self: SessionManager, key: str, session: "_Session", pct: float) -> str:
    """Delegate in-place compaction under turn exclusion."""
    return await self._compaction._compact_in_place(key, session, pct)


def _settle_compact_cooldown(
    self: SessionManager, key: str, provider: LLMProvider, pct_before: float
) -> bool:
    """Delegate measurable/deferred compaction verdict settlement."""
    return self._compaction._settle_compact_cooldown(key, provider, pct_before)


def _judge_compact_effect(
    self: SessionManager, key: str, pct_before: float, pct_after: float
) -> bool:
    """Delegate compaction effectiveness and damping policy."""
    return self._compaction._judge_compact_effect(key, pct_before, pct_after)


async def _reset_still_critical(
    self: SessionManager,
    key: str,
    pct_before: float,
    pct_after: float,
    *,
    expect: "_Session | None",
) -> bool:
    """Delegate guarded reset after an ineffective compaction."""
    return await self._compaction._reset_still_critical(key, pct_before, pct_after, expect=expect)


async def _fire_compact_callback(
    self: SessionManager, key: str, pct: float, *, success: bool, outcome: str | None = None
) -> None:
    """Delegate compaction callback dispatch."""
    await self._compaction._fire_compact_callback(key, pct, success=success, outcome=outcome)


# The replay-suppression pair closes this module, ``consume_replay_suppression`` last:
# ``test/test_app_slot_ownership.py`` reads each one as its text from ``def`` onward.
def _consume_persisted_replay_suppression(self: SessionManager, key: str, folded: str) -> bool:
    """Read *and clear* the on-disk half of the suppression, for either alias.

    The in-memory set cannot be the whole answer. Its two writers are different
    processes — ``kirocrew app uninstall`` has no live manager at all — and it
    does not survive a gateway restart, so a reinstall after either would replay
    the removed app's transcript with nothing left to stop it. Cleared as it is
    read, matching the in-memory branch: the FIRST cold start after the uninstall
    starts empty, and a later idle-timeout expiry on that key does not.
    """
    found = False
    for candidate in (folded, key) if folded != key else (key,):
        if self._session_map.get_flag(candidate, SUPPRESS_REPLAY_FLAG):
            self._session_map.set_flag(candidate, SUPPRESS_REPLAY_FLAG, False)
            found = True
    # `set_flag` schedules the debounced flush rather than forcing one. A crash
    # inside that window leaves the flag set, and the next cold start on this key
    # then starts empty a second time — recoverable, and the transcript is intact
    # on disk. Forcing a synchronous write here would put a disk write on the
    # event loop on every cold start after an uninstall, which is the larger cost.
    return found


def consume_replay_suppression(self: SessionManager, key: str) -> bool:
    """Read *and clear* whether *key*'s next cold start must skip replay.

    One-shot by construction, the same shape as
    :meth:`consume_needs_reinjection`: the flag is cleared as it is read, so
    exactly the FIRST cold start after ``discard_conversation(replay=False)``
    starts empty. Leaving it set would make every later cold start on that
    key — an idle-timeout expiry, a gateway restart — silently amnesiac,
    which nobody asked for.
    """
    folded = self._fold_key(key)
    # BOTH markers are consumed, and the persisted one unconditionally — never as
    # the `elif` tail of the in-memory branch. A gateway uninstall sets both (an
    # in-memory one via `discard_conversation(replay=False)` and a durable one via
    # `suppress_replay_persistently`), so a chain that stops at the first hit
    # leaves the disk flag standing and a LATER cold start of the NEW installation
    # starts empty for no reason — turning a fix for stale history into silent
    # amnesia about live history.
    persisted = self._consume_persisted_replay_suppression(key, folded)
    in_memory = False
    if key in self._suppress_replay:
        self._suppress_replay.discard(key)
        in_memory = True
    elif folded in self._suppress_replay:
        self._suppress_replay.discard(folded)
        in_memory = True
    if not (in_memory or persisted):
        return False
    # The session that consumed the suppression starts with no history, so
    # its first confirmed reading is this key's floor. Marked here, on the
    # session itself, because this is the one place that knows the replay
    # was actually skipped -- and the flag then dies with the session.
    session = self._sessions.get(folded) or self._sessions.get(key)
    if session is not None:
        session.floor_pending = True
    return True
