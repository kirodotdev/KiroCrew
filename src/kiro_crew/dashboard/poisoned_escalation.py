"""The poisoned-conversation escalation of a dashboard chat slot, in one place.

A persisted native conversation the backend keeps rejecting before it streams
anything is "poisoned": every retry into it fails the same way while a brand-new
conversation on the same gateway and model answers. The chat runner recovers it
by discarding the native conversation (the resume SID only, so the session-map
entry and its channel linkage survive) and replaying the turn once on a fresh
one. Two other recoveries reuse the same discard and the same one-shot: an
image retained in the native history that the backend rejects, and
preserved thinking bound to another conversation.

The state crosses turns and is two cells on the slot:

* ``_prestream_exhausted_cycles`` -- the streak of consecutive cycles that
  reached the terminal error with a transient error and no model activity, that
  is, a whole retry ladder exhausted before anything streamed;
* ``_poisoned_reset_used`` -- the one-shot a discard spends. Only a landed turn
  re-arms it, so a genuine prolonged outage gets one fresh conversation, never a
  discard loop.

The cells stay slot fields, initialised by the slot's constructor. Every
transition goes through :class:`PoisonedConversationEscalation`, the
recovery-replay ledger's refund included: its policy asks for the refund
(``recovery_replays.ReplayPolicy.refunds_discard_one_shot``) and the runner's
re-arm calls :meth:`~PoisonedConversationEscalation.refund_one_shot`. Nothing
else in the package writes either cell or names one in a string, which
``test/test_poisoned_escalation.py`` pins.

The rule, as the runner drives it:

1. A terminal failure counts toward the streak only when it is transient and
   nothing streamed (:meth:`~PoisonedConversationEscalation.record_terminal_failure`);
   any other terminal failure breaks the streak. Consecutive means consecutive.
2. Model activity on any path, a cancelled turn included, breaks the streak: the
   backend served this conversation
   (:meth:`~PoisonedConversationEscalation.record_activity`).
3. At :data:`POISONED_SESSION_CYCLES` with the one-shot unspent, a canary probe
   is due (:meth:`~PoisonedConversationEscalation.should_probe`). The runner
   runs it on a fresh background conversation; only a canary that answers is
   evidence the rejection is specific to this conversation. A Stop initiated
   while the probe runs vetoes the discard; the runner compares the Stop
   counters it read before the probe, which are locals of that turn.
4. A discard spends the one-shot
   (:meth:`~PoisonedConversationEscalation.arm_discard`). The canary's keeps the
   streak; the image-history and thinking-binding discards clear it, since
   neither is an outage cycle.
5. A landed turn breaks the streak and re-arms the one-shot
   (:meth:`~PoisonedConversationEscalation.record_landed_turn`). A cancelled turn
   never re-arms it: a Stop proves nothing about the conversation's health.
6. A cancelled image-history or thinking-binding replay re-arms the one-shot and
   leaves the streak (:meth:`~PoisonedConversationEscalation.refund_one_shot`):
   the discard spent it, but the turn it was spent for never ran.

Whether the turn's own ``finally`` discards the conversation is that turn's
decision, so it stays a local of the turn; this object holds what outlives it.
"""

from __future__ import annotations

from typing import Any

#: Consecutive pre-stream-exhausted cycles (TRANSIENT_RETRIES + 1 attempts, zero
#: output) before the terminal error branch stops advising "retry in a moment" and
#: probes for a poisoned conversation instead. Two cycles mean at least
#: 2×(TRANSIENT_RETRIES+1) consecutive pre-stream failures spanning a user action
#: (Continue or a new message), which a momentary capacity blip does not survive
#: but a backend-rejected persisted conversation always does.
POISONED_SESSION_CYCLES = 2


class PoisonedConversationEscalation:
    """The streak and one-shot transitions over one slot's two cells.

    Holds no state of its own: it reads and writes the slot it was built over, so
    a value built anywhere sees the same escalation.
    """

    __slots__ = ("_slot",)

    def __init__(self, slot: Any) -> None:
        self._slot = slot

    @property
    def exhausted_cycles(self) -> int:
        """Consecutive pre-stream-exhausted cycles so far."""
        cycles: int = self._slot._prestream_exhausted_cycles
        return cycles

    @property
    def one_shot_spent(self) -> bool:
        """True once a discard spent the one-shot and no turn has landed since."""
        spent: bool = self._slot._poisoned_reset_used
        return spent

    def record_terminal_failure(self, *, emitted: bool, thought: bool, transient: bool) -> None:
        """Count a cycle that reached the terminal error branch.

        Only a transient error with no reply, tool call or thinking extends the
        streak; that is a full retry ladder exhausted before the backend streamed
        anything. Any other terminal failure breaks it.
        """
        if transient and not emitted and not thought:
            self._slot._prestream_exhausted_cycles += 1
        else:
            self._slot._prestream_exhausted_cycles = 0

    def record_activity(self, *, emitted: bool, thought: bool) -> None:
        """Break the streak when the turn produced output or streamed thinking.

        Called on every exit of the turn, so the recovery paths that return early
        and a cancelled turn are covered too. It never re-arms the one-shot.
        """
        if emitted or thought:
            self._slot._prestream_exhausted_cycles = 0

    def record_landed_turn(self) -> None:
        """A turn that reached the model and completed: reset the streak and re-arm."""
        self._slot._prestream_exhausted_cycles = 0
        self._slot._poisoned_reset_used = False

    def should_probe(self) -> bool:
        """True when the streak reached the threshold and the one-shot is unspent.

        Read right after :meth:`record_terminal_failure`: a cycle that did not
        count leaves the streak at zero, so a probe is only ever due on an
        exhausted one.
        """
        return self.exhausted_cycles >= POISONED_SESSION_CYCLES and not self.one_shot_spent

    def arm_discard(self, *, clear_streak: bool) -> None:
        """Spend the one-shot for a discard the caller is about to perform.

        ``clear_streak`` is for a discard that is not an outage cycle (an
        image-history or thinking-binding rebuild), so none of the streak carries
        into the rebuilt conversation.
        """
        self._slot._poisoned_reset_used = True
        if clear_streak:
            self._slot._prestream_exhausted_cycles = 0

    def refund_one_shot(self) -> None:
        """Re-arm the one-shot for a discard whose replay was cancelled before it ran.

        The recovery-replay ledger asks for this when a Stop, a newer message or a
        rebind cancels an image-history or thinking-binding replay. The streak is
        left as it is.
        """
        self._slot._poisoned_reset_used = False
