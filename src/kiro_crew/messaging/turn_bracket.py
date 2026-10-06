"""The turn bracket: what one turn owes its session once it has stopped.

Every engine that drives a model turn consumes two pieces of one-shot session
state before the prompt goes out, and must settle both on EVERY exit path:

* the post-compaction re-injection flag, read-and-cleared before
  ``build_message`` so the compacted session gets its session-start context back
  exactly once, and put back when the turn never lands;
* the skill-body dedup writes ``build_message`` records at build time, committed
  when the prompt reached the window and rolled back when it did not.

:class:`TurnBracket` holds that bookkeeping for one turn. It consumes per
attempt (a turn replayed after a failed compaction re-consumes, and settles on
the latest attempt's value), it re-arms before it rolls back, and it settles
once: :meth:`TurnBracket.settle` is idempotent, so an engine that hands a turn
over early (a cron job retrying itself) settles at the hand-off and its later
``finally`` is a no-op.

The bracket never acquires, releases, charges or logs a session: the engine
that claimed the session's permit owns those steps, so nothing here can release
what it did not acquire. :class:`kiro_crew.messaging.dispatch.ChannelTurns` is
the channel turn's engine; engines not yet on it use the bracket in place.

Every helper is defensive on the accessor it reads, in the fail-safe direction:
a session or context-builder stand-in that predates a method gets a no-op, never
an ``AttributeError`` on a real inbound message.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from kiro_crew.agent_sdk.drivers.acp_vocab import classify_stop_reason

logger = logging.getLogger(__name__)


def consume_reinjection(sessions: Any, session_key: str) -> bool:
    """Read-and-clear the one-shot post-compaction re-injection flag.

    ``session_compaction`` marks it after a successful in-place compaction,
    because compaction drops the session-start context (skills index, member
    section, response preferences). The turn that consumes it passes the value
    to ``build_message`` as ``needs_reinjection`` so that context comes back
    exactly once. Every channel turn loop reads it through this one helper: a
    per-channel copy of the turn loop that skips it re-injects nothing after
    ``/compact``.

    Defensive on the accessor: a session stand-in that predates the flag gets
    the safe ``False``, never an AttributeError on a real inbound message.
    """
    consume = getattr(sessions, "consume_needs_reinjection", None)
    return bool(consume(session_key)) if callable(consume) else False


def stop_reason_landed(stop_reason: str | None) -> bool:
    """Whether the turn that ended with *stop_reason* landed, for re-injection.

    ``None`` means no completion was observed at all -- the stream exhausted or
    was cut without an ``EVENT_COMPLETE`` -- and that is never landed: nothing
    proves the prompt reached the conversation. A string is a completion's
    stop reason, judged as an allowlist through the one stop-reason classifier
    every completion consumer shares: only a ``succeeded`` class (``end_turn``,
    or an empty reason from a provider that never populates the field) proves
    the prompt -- and the re-injected context it carried -- is now part of the
    conversation. Every other terminal is a turn the backend did not complete:
    ``cancelled`` (the backend drops a cancelled turn from its transcript),
    ``stale_recover`` and ``error: tool stall`` (synthetic completions for a
    wedged turn), ``refusal`` and the ``error:`` family. All of those leave the
    consumed flag to be re-armed.
    """
    if stop_reason is None:
        return False
    return classify_stop_reason(stop_reason).is_success


def driver_turn_landed(driver: Any) -> bool:
    """:func:`stop_reason_landed` for a completed ``TurnDriver.run``.

    ``run`` returns normally on every terminal the backend synthesises a
    completion for, a user cancel included, and also when the stream simply
    ends without one, so the driver records both the stop reason and whether a
    completion was observed. Defensive on the attributes, like every other
    read on the driver seam, in the fail-safe direction: a stand-in that
    reports no completion is not landed, so the worst case is one extra
    re-injection rather than a lost one.
    """
    if not getattr(driver, "completion_observed", False):
        return stop_reason_landed(None)
    return stop_reason_landed(getattr(driver, "last_stop_reason", "") or "")


def rearm_reinjection(sessions: Any, session_key: str, *, consumed: bool, landed: bool) -> None:
    """Put the one-shot flag back when this turn consumed it but never landed.

    The flag is cleared BEFORE ``build_message``, so a turn that then dies -- a
    provider error, a driver fault, a cancel -- has discarded the prompt that
    carried the re-injected context, and without this the session runs without
    its skills index (and a member DM without its rules) until the next
    compaction. This is the contract the dashboard runner already keeps in its
    own ``finally`` (``chat_runner``: re-arm when consumed and not landed); the
    channel loops share it so the two paths cannot disagree.

    ``landed`` means the turn was recorded a success. A cancelled turn is NOT
    landed: the backend drops a cancelled turn from its own transcript, so the
    context it carried is gone with it. Call from the turn's ``finally`` so every
    exit path is covered. Never raises: a failure to re-arm is logged and the
    turn's own outcome stands.
    """
    if not consumed or landed:
        return
    mark = getattr(sessions, "mark_needs_reinjection", None)
    if not callable(mark):
        return
    try:
        mark(session_key)
    except Exception:
        logger.debug(
            "re-arming post-compaction re-injection failed session=%s",
            session_key,
            exc_info=True,
        )


def rollback_skill_bodies(ctx_builder: Any, session_key: str, *, landed: bool) -> None:
    """Settle this turn's build-time skill-body dedup writes at the turn seam.

    Companion to :func:`rearm_reinjection` at the same turn ``finally`` seam.
    ``build_message`` records injected skill bodies at build time so the dedup
    holds at every caller and stashes an undo entry for the current build. This
    settles that entry exactly once per turn:

    * ``landed`` — the prompt reached the provider window, so commit: drop the
      undo entry (the bodies are in the window and must not be rolled back
      later). Leaving it armed would let a later non-landing turn roll back this
      LANDED build, re-injecting bodies the window already holds.
    * not ``landed`` — a provider error, cancel or driver fault discarded the
      prompt, so roll back: restore the pre-build state so the next turn
      re-injects the bodies as full bodies rather than demoting to pointers.

    A turn that built no context finds no armed undo entry (a landed build
    cleared its own, and turns on one session key are serialized) so both calls
    are no-ops. Never raises: a failed settle is logged and the record's own
    fail-safe (a pointer next turn, not silence) still holds. Defensive on the
    accessors so a builder stand-in that predates the methods is a safe no-op.
    """
    method = "commit_skill_bodies" if landed else "rollback_skill_bodies"
    settle = getattr(ctx_builder, method, None)
    if not callable(settle):
        return
    try:
        settle(session_key)
    except Exception:
        logger.debug(
            "settling skill-body dedup state failed (landed=%s) session=%s",
            landed,
            session_key,
            exc_info=True,
        )


class TurnBracket:
    """One turn's re-injection and skill-body bookkeeping, settled once.

    Interface, in the order an engine uses it:

    * :meth:`take_reinjection` -- read-and-clear the re-injection flag NOW and
      return it for ``build_message``. Call it once per ATTEMPT: a replayed
      attempt consumes again, and :meth:`settle` re-arms on the latest value.
    * :meth:`landed` -- record whether the turn landed, from whatever evidence
      the engine holds: a ``bool`` as given, a stop reason (``str``, or ``None``
      for "no completion observed") through :func:`stop_reason_landed`, or a
      finished ``TurnDriver`` through :func:`driver_turn_landed`. Not called
      means not landed, which is the fail-safe reading for an exit by exception.
    * :meth:`settle` -- re-arm the flag when it was taken and the turn did not
      land, THEN commit or roll back the skill bodies. Only the first call acts,
      and it never raises.
    """

    def __init__(self, sessions: Any, ctx_builder: Any, session_key: str) -> None:
        self._sessions = sessions
        self._ctx_builder = ctx_builder
        self._session_key = session_key
        self._consumed = False
        self._landed = False
        self._settled = False

    def take_reinjection(self) -> bool:
        self._consumed = consume_reinjection(self._sessions, self._session_key)
        return self._consumed

    def landed(self, evidence: Any) -> bool:
        if isinstance(evidence, bool):
            self._landed = evidence
        elif evidence is None or isinstance(evidence, str):
            self._landed = stop_reason_landed(evidence)
        else:
            self._landed = driver_turn_landed(evidence)
        return self._landed

    def settle(self) -> None:
        if self._settled:
            return
        self._settled = True
        rearm_reinjection(
            self._sessions, self._session_key, consumed=self._consumed, landed=self._landed
        )
        rollback_skill_bodies(self._ctx_builder, self._session_key, landed=self._landed)


@asynccontextmanager
async def turn_bracket(
    sessions: Any, ctx_builder: Any, session_key: str
) -> AsyncIterator[TurnBracket]:
    """:class:`TurnBracket` for an engine whose whole turn sits in one block.

    The exit settles, on every path, so the block cannot forget to. An engine
    that must settle at a precise point of its own ``finally`` (before it
    finalizes a renderer, say) constructs the bracket directly instead.
    """
    bracket = TurnBracket(sessions, ctx_builder, session_key)
    try:
        yield bracket
    finally:
        bracket.settle()
