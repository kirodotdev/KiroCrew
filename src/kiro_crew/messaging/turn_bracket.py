"""The turn bracket: what one turn owes its session once it has stopped.

Every engine that drives a model turn consumes two pieces of one-shot session
state before the prompt goes out, and must settle both on EVERY exit path:

* the post-compaction re-injection flag, read-and-cleared before
  ``build_message`` so the compacted session gets its session-start context back
  exactly once, put back when the turn never lands, and armed when the backend
  compacted the session during the turn;
* the skill-body dedup writes ``build_message`` records at build time, committed
  when the prompt reached the window and rolled back when it did not.

A channel engine that builds with ``tab_notices`` settles a third on the same
verdict: the sub-agent notices its build read from the conversation's dashboard
tab leave the tab's queue only when the turn landed.

The channel engine also reads-and-clears the cancelled-turn flag a Stop sets
(:meth:`TurnBracket.take_cancelled_turn`), once per message, and gives it back
when the build never admitted the prompt.

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

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from kiro_crew.agent_sdk.drivers.acp_vocab import classify_stop_reason
from kiro_crew.parent_turns import note_turn_landed

logger = logging.getLogger(__name__)


def consume_reinjection(sessions: Any, session_key: str) -> bool:
    """Read-and-clear the one-shot post-compaction re-injection flag.

    ``session_compaction`` marks it after a successful in-place compaction, and
    :func:`rearm_reinjection` marks it after a turn whose stream reported that
    the backend compacted the session, because compaction drops the session-start
    context (skills index, member section, response preferences). The turn that
    consumes it passes the value
    to ``build_message`` as ``needs_reinjection`` so that context comes back
    exactly once. Every channel turn loop reads it through this one helper: a
    per-channel copy of the turn loop that skips it re-injects nothing after
    ``/compact``.

    Defensive on the accessor: a session stand-in that predates the flag gets
    the safe ``False``, never an AttributeError on a real inbound message.
    """
    consume = getattr(sessions, "consume_needs_reinjection", None)
    return bool(consume(session_key)) if callable(consume) else False


def rearm_first_turn(
    sessions: Any, session_key: str, provider: Any, *, is_new: bool, resumed: bool
) -> None:
    """Give back the first-turn observation a claim took, when its prompt never went out.

    ``get_or_create`` hands a new session's first turn to the claim that takes it.
    A turn that then gives up before sending its prompt (the busy memory pool never
    admitted its build) must hand it back while it still holds the session, or
    the next message reads as an existing session and loses its session-start
    context. Defensive on the accessor, like :func:`consume_reinjection`.
    """
    rearm = getattr(sessions, "rearm_first_turn", None)
    if is_new and callable(rearm):
        rearm(session_key, provider, resumed=resumed)


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


def rearm_reinjection(
    sessions: Any,
    session_key: str,
    *,
    consumed: bool,
    landed: bool,
    compacted: bool = False,
) -> None:
    """Leave the one-shot flag set for the next turn when that turn needs it.

    Either reason is enough:

    * this turn consumed the flag but never landed. The flag is cleared BEFORE
      ``build_message``, so a turn that dies discarded the re-injected context, and
      the session would run without its skills index (a member DM without its
      rules) until the next compaction. Same contract as ``chat_runner``'s
      ``finally``.
    * ``compacted``: the backend reported a ``completed`` compaction this turn. A
      backend compacting its own window drops the session-start context, landed or
      not, so the next turn must send it again.

    ``landed`` means the turn was recorded a success; a cancelled turn is NOT
    landed, since the backend drops it from its transcript. Call from the turn's
    ``finally``. Never raises.
    """
    if not compacted and (not consumed or landed):
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


def rollback_skill_bodies(
    ctx_builder: Any,
    session_key: str,
    *,
    landed: bool,
    tab_notices: list[Callable[[bool], None]] | None = None,
) -> None:
    """Settle this turn's build-time skill-body dedup writes at the turn seam.

    ``build_message`` records injected skill bodies at build time and stashes an
    undo entry. This settles it once per turn:

    * ``landed``: commit (drop the undo entry), or a later failed turn would roll
      back bodies the window already holds.
    * not ``landed``: roll back, so the next turn re-injects full bodies instead of
      pointers.

    *tab_notices*: the list this turn passed to ``build_message`` as its
    ``tab_notices``. The dashboard-tab notices its build read settle on the same
    verdict through the call it holds, so a turn settles only what its own build
    read, never another turn's on the same session.

    With no armed entry both are no-ops. Never raises; a builder without the
    methods is a no-op.
    """
    for settle_notices in tab_notices or ():
        try:
            settle_notices(landed)
        except Exception:
            logger.debug("settling tab notices failed session=%s", session_key, exc_info=True)
    if tab_notices:
        tab_notices.clear()
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


def _live_session(sessions: Any, session_key: str) -> Any:
    """The session record *session_key* folds onto, or ``None``. Defensive on every accessor."""
    getter = getattr(getattr(sessions, "_sessions", None), "get", None)
    if not callable(getter):
        return None
    fold = getattr(sessions, "_fold_key", None)
    try:
        return getter(fold(session_key) if callable(fold) else session_key)
    except Exception:
        return None


def typed_text_span(preamble: str, text: str) -> dict[str, Any]:
    """``build_message``'s ``user_text_range`` for ``f"{preamble}\\n\\n{text}"``, or ``{}``.

    Only the typed text is the person's: quick prompts, image paths and skill
    triggers must not read the restored turn put in front of it.
    """
    if not preamble:
        return {}
    start = len(preamble) + 2
    return {"user_text_range": (start, start + len(text))}


def arm_cancelled_turn(sessions: Any, session_key: str, outcome: Any) -> None:
    """Set ``prev_turn_cancelled`` when a channel Stop's cancel reached a turn.

    The Stop paths that cancel the provider directly, not through
    ``SessionManager.stop_turn``, hand over what ``cancel`` returned. Only
    ``"acked"`` and ``"timeout"`` (written, ack still pending) stopped a turn; a
    ``"no_turn"`` or ``"error"`` must not label a finished turn as interrupted.
    """
    if outcome not in ("acked", "timeout"):
        return
    session = _live_session(sessions, session_key)
    if session is not None:
        session.prev_turn_cancelled = True


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
      A landed verdict is also noted on the session (:func:`note_turn_landed`).
    * :meth:`compacted` -- record the latest attempt's ``TurnDriver``
      ``compaction_completed``; it is kept current, so reading it in ``finally``
      covers an exit by exception. Not called means no compaction.
    * :meth:`settle` -- re-arm the flag when it was taken and the turn did not
      land, or when the backend compacted the session, THEN commit or roll back
      the skill bodies and settle the tab notices the build read. Only the first
      call acts, and it never raises.
    """

    def __init__(self, sessions: Any, ctx_builder: Any, session_key: str) -> None:
        self._sessions = sessions
        self._ctx_builder = ctx_builder
        self._session_key = session_key
        self._consumed = False
        self._landed = False
        self._compacted = False
        self._settled = False
        self._cancelled_from: Any = None
        #: Passed to ``build_message`` as ``tab_notices``; settled with the turn.
        self.tab_notices: list[Callable[[bool], None]] = []

    def take_reinjection(self) -> bool:
        self._consumed = consume_reinjection(self._sessions, self._session_key)
        return self._consumed

    async def take_cancelled_turn(self) -> str:
        """Read-and-clear ``prev_turn_cancelled``; return the restore to prepend, or ``""``.

        A Stop sets the flag because the backend drops the stopped turn from its own
        log. The restore re-sends that turn's request and partial reply from the
        conversation log, which holds only earlier turns while this one is being built.
        ``is not True`` so a mock session's truthy attribute is not a Stop.
        """
        session = _live_session(self._sessions, self._session_key)
        if session is None or getattr(session, "prev_turn_cancelled", False) is not True:
            return ""
        session.prev_turn_cancelled = False
        self._cancelled_from = session
        conversation_log = getattr(self._ctx_builder, "conversation_log", None)
        if not conversation_log:
            return ""
        # Deferred: the channel Stop paths import this module through
        # session_compaction, which must not pay context's import cost.
        from kiro_crew.context import build_cancelled_turn_preamble

        preamble = await asyncio.to_thread(
            build_cancelled_turn_preamble,
            conversation_log,
            self._session_key,
            newest_request=True,
        )
        return preamble if isinstance(preamble, str) else ""

    def rearm_cancelled_turn(self) -> None:
        """Give the flag back when nothing reached the model, on the session it came from."""
        session = self._cancelled_from
        if session is not None and _live_session(self._sessions, self._session_key) is session:
            session.prev_turn_cancelled = True

    def landed(self, evidence: Any) -> bool:
        if isinstance(evidence, bool):
            self._landed = evidence
        elif evidence is None or isinstance(evidence, str):
            self._landed = stop_reason_landed(evidence)
        else:
            self._landed = driver_turn_landed(evidence)
        if self._landed:
            note_turn_landed(self._sessions, self._session_key)
        return self._landed

    def compacted(self, evidence: Any) -> bool:
        # ``is True``, not truthiness: a mock stand-in answers any attribute
        # with a truthy mock, which is not a reported compaction.
        self._compacted = getattr(evidence, "compaction_completed", False) is True
        return self._compacted

    def settle(self) -> None:
        if self._settled:
            return
        self._settled = True
        rearm_reinjection(
            self._sessions,
            self._session_key,
            consumed=self._consumed,
            landed=self._landed,
            compacted=self._compacted,
        )
        rollback_skill_bodies(
            self._ctx_builder, self._session_key, landed=self._landed, tab_notices=self.tab_notices
        )


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
