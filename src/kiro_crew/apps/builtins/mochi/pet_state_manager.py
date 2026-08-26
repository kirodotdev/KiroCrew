"""Pet state manager for Mochi — walking buffer, error auto-recovery, stats.

Ported from ``src/main/petStateManager.ts``. Wraps the pure transition table
(:mod:`pet_state_machine`) with the stateful behaviours around it:

* While walking, requested states are BUFFERED (``pending``) and applied when
  the walk finishes, so a think/work state landing mid-walk is not lost.
* ``finish_walking`` restores instantly for very short walks (< 300ms — the
  target was the current position) and after a 200ms delay otherwise, to
  avoid an idle flash between chained walks.
* Entering ``error`` arms a 3s auto-recovery (``timeout`` event).
* thinking-state edges feed the stats service (start/end of thinking time).

PORTING NOTES
-------------

Owner-driven timers, injected clock (the ``tick(now_ms)`` pattern): the error
auto-recovery and the 200ms walk-restore delay are deadlines fired by
``tick``. Both share the single ``state_timer`` slot, exactly like the
original's one ``stateTimer`` handle — arming either cancels the other, which
is load-bearing (``start_walking`` cancels a pending error recovery).

The stats coupling is injected (the original imported the ``statsService``
singleton; the port takes an optional ``StatsService`` — one more singleton
removed under the deviations policy).

Pinned by ``test/test_mochi_small_modules.py``.
"""

from __future__ import annotations

from typing import Any, Callable

from kiro_crew.apps.builtins.mochi.pet_state_machine import ERROR_TIMEOUT_MS, transition

# Delay before restoring the buffered state after a normal walk.
WALK_RESTORE_DELAY_MS = 200

# Walks shorter than this restore instantly (no next walk to chain).
INSTANT_WALK_THRESHOLD_MS = 300

PET_STATE_CHANGE_CHANNEL = "pet:state-change"
PET_MOOD_CHANNEL = "mochi:mood"

# Mood is expression, orthogonal to behaviour state (pet_state_machine owns the
# latter). Verified against the original shared/types.ts:15 and renderer
# useMood.ts: transient moods self-clear after MOOD_DURATION_MS; persistent ones
# (sleepy/busy) linger until an explicit clear.
ALL_MOODS = ("neutral", "happy", "sleepy", "curious", "busy", "scared")
TRANSIENT_MOODS = frozenset({"happy", "scared", "curious"})
MOOD_DURATION_MS = 3000

BroadcastFn = Callable[..., None]


class PetStateManager:
    """Behaviour state with walking buffer and timed recoveries."""

    def __init__(
        self,
        broadcast: BroadcastFn,
        stats: Any | None = None,
    ) -> None:
        self._broadcast = broadcast
        self._stats = stats
        self._pet_state = "offline"
        self._pending_state: str | None = None
        # One shared timer slot: EITHER the error auto-recovery OR the
        # walk-restore delay (arming one cancels the other, like the original).
        self._timer_deadline: int | None = None
        self._timer_action: str | None = None  # "error-recover" | "walk-restore"
        self._walk_started_at = 0
        self._mood = "neutral"
        # Mood auto-reset runs on its OWN deadline: a transient mood can be
        # showing while the behaviour state is (e.g.) error, so it must not share
        # the single state-timer slot above.
        self._mood_deadline: int | None = None

    @property
    def current(self) -> str:
        return self._pet_state

    @property
    def current_mood(self) -> str:
        return self._mood

    def set_pet_state(self, new_state: str, now_ms: int) -> None:
        if self._pet_state == "walking" and new_state != "walking":
            self._pending_state = new_state
            return
        prev_state = self._pet_state
        self._pet_state = new_state
        self._pending_state = None
        self._broadcast(PET_STATE_CHANGE_CHANNEL, self._pet_state)

        # The original cleared any lingering persistent mood the moment the pet
        # began thinking/walking (renderer useMood.clearPersistentMood); keep
        # that coupling here so the backend owns both state and mood.
        if new_state in ("thinking", "walking"):
            self.clear_persistent_mood(now_ms)

        # Thinking-state edges feed the stats service.
        if self._stats is not None:
            if new_state == "thinking" and prev_state != "thinking":
                self._stats.record_thinking_start(now_ms)
            elif prev_state == "thinking" and new_state != "thinking":
                self._stats.record_thinking_end(now_ms)

        self._clear_timer()
        if new_state == "error":
            self._timer_deadline = now_ms + ERROR_TIMEOUT_MS
            self._timer_action = "error-recover"

    def start_walking(self, now_ms: int) -> None:
        self._clear_timer()
        if self._pet_state != "walking":
            self._pending_state = self._pet_state
        self._pet_state = "walking"
        self._walk_started_at = now_ms
        self._broadcast(PET_STATE_CHANGE_CHANNEL, "walking")
        self.clear_persistent_mood(now_ms)

    def finish_walking(self, now_ms: int) -> None:
        walk_duration = now_ms - (self._walk_started_at or 0)
        self._walk_started_at = 0

        if walk_duration < INSTANT_WALK_THRESHOLD_MS:
            # Instant walk — restore immediately, no idle flash possible.
            self._restore_from_walk(now_ms)
            return

        # Normal walk — delay so a chained walk can start without a flash.
        self._clear_timer()
        self._timer_deadline = now_ms + WALK_RESTORE_DELAY_MS
        self._timer_action = "walk-restore"

    def apply_event(self, event: str, now_ms: int) -> None:
        self.set_pet_state(transition(self._pet_state, event), now_ms)

    def set_mood(self, mood: str, now_ms: int) -> None:
        """Set the expression mood and broadcast it.

        Transient moods (happy/curious/scared) arm a self-reset; persistent ones
        (sleepy/busy) linger until :meth:`clear_persistent_mood`. The reset fires
        from :meth:`tick`, so at the owner loop's 1s cadence the visible duration
        is MOOD_DURATION_MS rounded up to the next tick — fine for an expression.
        """
        self._mood = mood
        self._broadcast(PET_MOOD_CHANNEL, mood)
        # This manager is the single source of truth for mood, so it is also the
        # only place the mood histogram can be kept. Without this the Memories
        # view's "top moods" row was permanently empty.
        if self._stats is not None and mood != "neutral":
            try:
                self._stats.record_mood(mood, now_ms)
            except Exception:  # noqa: BLE001 -- a stat must never break the pet
                pass
        if mood != "neutral" and mood in TRANSIENT_MOODS:
            self._mood_deadline = now_ms + MOOD_DURATION_MS
        else:
            self._mood_deadline = None

    def clear_persistent_mood(self, now_ms: int) -> None:
        """Reset a lingering PERSISTENT mood (sleepy/busy) to neutral.

        A transient mood is left to its own timer. Mirrors the renderer's
        clearPersistentMood, which the original fired on thinking/walking.
        """
        if self._mood != "neutral" and self._mood not in TRANSIENT_MOODS:
            self.set_mood("neutral", now_ms)

    def tick(self, now_ms: int) -> None:
        """Fire the pending timer if due (error recovery or walk restore)."""
        # Mood auto-reset runs on an independent deadline.
        if self._mood_deadline is not None and now_ms >= self._mood_deadline:
            self._mood_deadline = None
            self.set_mood("neutral", now_ms)
        if self._timer_deadline is None or now_ms < self._timer_deadline:
            return
        action = self._timer_action
        self._clear_timer()
        if action == "error-recover":
            self.set_pet_state(transition(self._pet_state, "timeout"), now_ms)
        elif action == "walk-restore":
            self._restore_from_walk(now_ms)

    def destroy(self) -> None:
        self._clear_timer()
        self._mood_deadline = None

    # ── Internal ───────────────────────────────────────────────────────────

    def _restore_from_walk(self, now_ms: int) -> None:
        self._pet_state = "idle"
        restore = self._pending_state or "idle"
        self._pending_state = None
        self.set_pet_state(restore, now_ms)

    def _clear_timer(self) -> None:
        self._timer_deadline = None
        self._timer_action = None
