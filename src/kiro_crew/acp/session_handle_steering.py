"""Steering a live turn.

The steer entry, kiro-cli's request to drop the steers a turn has not read, the codex
steering-request protocol, settling abandoned answers, and the injected, proven and
denied steer bookkeeping the dispatch loop reads.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_abandoned_steering``, ``_goose_active_run_id``,
``_goose_steers``, ``_held_steers_unanswered``, ``_steering_answers``,
``_steering_cancel_tasks``, ``_steering_settled``, ``_steers_accepted``,
``_steers_proven``, ``_turn_steering_denied``.

Instance fields read: ``_awaiting_permission``, ``_batch_window``, ``_cancelled``,
``_prompt_starts``, ``_prompt_written``, ``_runtime``, ``_session_id``, ``_turn_done``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import batch_guard
from kiro_crew.acp.session_handle_runtime_contract import AcpRuntimeDead
from kiro_crew.acp.types import (
    ACP_BACKENDS_STEER_RETRACT,
    ACP_BACKENDS_STEERING_REQUEST,
    EVENT_STEER_CONSUMED,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
    METHOD_CANCEL,
    METHOD_GOOSE_SESSION_STEER,
    METHOD_SESSION_STEER_CLEAR,
    METHOD_SESSION_STEERING,
    OWN_STEERING_REQUEST_METHODS,
    STEER_CLEAR_ANSWER_WAIT_SECS,
    STEERING_ANSWER_WAIT_SECS,
    STEERING_INJECTED,
    STEERING_STARTED_NEW_TURN,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
    goose_announced_run,
    goose_steer_echo_id,
    steering_advertised,
    steering_outcome,
    wrap_user_message,
)
from kiro_crew.agent_sdk.steer_origin import STEER_ORIGIN_USER, steer_params
from kiro_crew.tracing import steps as trace_steps

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


# How long a new prompt waits for codex steering answers still owed from the
# previous turn, so an adapter-owned turn one of them started is cancelled before
# our prompt goes out (``_settle_abandoned_steering``). The request's own wire and
# bounds are shared with ``AcpClient`` (see ``acp.types``).
_STEERING_SETTLE_SECS = 5.0


async def steer(self: AcpSessionHandle, message: str, *, origin: str = STEER_ORIGIN_USER) -> bool:
    """Inject a mid-turn steer into the running turn.

    Two transports. kiro-cli takes its ``_session/steer`` ext-method
    fire-and-forget (mirrors AcpClient.steer): the reply streams back inside
    the SAME in-flight session/prompt, the steering_consumed notification is
    the authoritative signal (surfaced as EVENT_STEER_CONSUMED), and the
    request response is not awaited. A backend in
    ``ACP_BACKENDS_STEERING_REQUEST`` (codex) is delivered by
    :meth:`_steer_via_steering_request`, which awaits only the adapter's
    answer: True means codex accepted the steer, not that the turn consumed
    it. Consumption is reported separately, as EVENT_STEER_CONSUMED at the
    turn's clean terminal, and a steer never reported consumed is queued by
    the caller. Returns False for an empty message or no active session on
    either transport.

    *origin* says who wrote the text (``STEER_ORIGIN_*``); only kiro's
    ``_session/steer`` carries it, and only when it is not the user's.
    """
    text = (message or "").strip()
    if not text or not self._session_id:
        return False
    if self._runtime.acp_backend in OWN_STEERING_REQUEST_METHODS:
        # goose takes its own request, not ``_session/steering``, which it never
        # advertises: without this a shared goose chat queued every steer.
        sent = await self._steer_via_goose_request(text)
        if sent:
            trace_steps.steered(trace_steps.STEER_INTERRUPT, source=self)
        return sent
    if self._runtime.acp_backend in ACP_BACKENDS_STEERING_REQUEST:
        if not steering_advertised(self.initialize_result):
            # The process's handshake did not advertise ``_session/steering``
            # (``_meta.steering.supported: true``): nothing is sent, and the
            # caller queues the text as the next turn.
            return False
        # A session whose own harness state carries the per-chat client's steering
        # floor (claude's ``ClaudeSharedSession``) is steered as that client steers.
        claude = callable(getattr(_harness_state(self), "_steering_adapter_floor_met", None))
        if claude and not _claude_steer_ready(self):
            # As the per-chat client: below the adapter release that takes the
            # request, or while a tool call of the turn is open (claude moves a
            # running tool to the background on a steer and ends the turn early),
            # nothing is sent and the caller queues the text as the next turn.
            # shortcut: the per-chat client holds such a steer until the tool
            # call ends; hold it here too if queueing proves too late.
            return False
        sent = await self._steer_via_steering_request(text, idle_prompt_required=claude)
        if sent:
            trace_steps.steered(trace_steps.STEER_INTERRUPT, source=self)
        return sent
    wrapped = wrap_user_message(text)
    await self._runtime.send_request(
        "_session/steer", steer_params(self._session_id, wrapped, origin)
    )
    batch_guard.note_prompt(getattr(self, "_batch_window", None), text)
    # Stamped HERE, at the innermost write, because this is the one point
    # every steer funnels through: the dashboard steers the inner client
    # directly while the IM transports steer the provider wrapper, and both
    # end up on this line. A reader of the stamp therefore needs no
    # per-transport wiring. See ``last_steer_monotonic``.
    self._last_steer_monotonic = time.monotonic()
    # The same funnel gives the steer its point span in the turn it went into
    # (tracing/steps.py): its mode, never its text.
    trace_steps.steered(trace_steps.STEER_INTERRUPT, source=self)
    return True


async def clear_pending_steers(self: AcpSessionHandle) -> bool | None:
    """Ask kiro-cli to drop every steer it holds that the turn has not read.

    Only a host in ``ACP_BACKENDS_STEER_RETRACT`` has the request; anywhere else,
    and with no session, nothing is sent and the answer is False. True means
    kiro-cli answered the request, which it does with ``{cleared: true}`` whether or
    not anything was pending. It is not evidence of a drop: that is the
    ``AgentExecutionUserMessageCleared`` notification which follows the answer and
    reaches the turn as ``EVENT_STEER_DROPPED``. False means the request was
    refused (a build without it answers ``-32601``), so nothing was dropped and the
    session stops offering it. None means no answer came within
    ``STEER_CLEAR_ANSWER_WAIT_SECS``, so whether anything was dropped is unknown.

    The answer is read off the session queue (``send_request_for_answer``), so the
    turn's own dispatch loop never sees it, and this call never waits on that loop.
    A runtime that dies under the write raises, as a steer's write does.
    """
    if (
        self._runtime.acp_backend not in ACP_BACKENDS_STEER_RETRACT
        or not self._session_id
        or self._steer_clear_refused
    ):
        return False
    answer = await self._runtime.send_request_for_answer(
        METHOD_SESSION_STEER_CLEAR, {"sessionId": self._session_id}
    )
    done, _ = await asyncio.wait({answer}, timeout=STEER_CLEAR_ANSWER_WAIT_SECS)
    if not done:
        self._runtime.forget_request(answer)
        return None
    if answer.cancelled():
        return None
    error = answer.exception()
    if isinstance(error, AcpRuntimeDead):
        return None
    if error is not None:
        # The class only, as for a refused steering request: the text is the
        # backend's.
        logger.info("steer clear on %s refused: %s", self._session_id, type(error).__name__)
        self._steer_clear_refused = True
        return False
    return True


def _harness_state(handle: AcpSessionHandle) -> Any:
    """This session's own harness state on the shared runtime, or None."""
    states = getattr(handle._runtime, "_session_harness_states", None) or {}
    return states.get(handle._session_id)


def _claude_steer_ready(handle: AcpSessionHandle) -> bool:
    """Whether a claude session of the shared runtime takes a steer now.

    Its adapter release takes ``_session/steering`` (the per-chat client's own
    floor, read on the session's harness state), and no tool call of the turn
    is open.
    """
    floor = getattr(_harness_state(handle), "_steering_adapter_floor_met", None)
    if not callable(floor) or floor() is not True:
        return False
    return handle._tool_tracker.current is None and not handle._tool_dispatched


async def _steer_via_steering_request(
    self: AcpSessionHandle, text: str, *, idle_prompt_required: bool = False
) -> bool:
    """Deliver a user steer over codex-acp's ``_session/steering`` request.

    *idle_prompt_required* (claude) asks the adapter to answer ``promptRequired``,
    and run nothing, when the steer reaches it after its turn settled, as the
    per-chat client asks.

    See ``ACP_BACKENDS_STEERING_REQUEST`` for what was measured. The adapter
    sends no ``steering_consumed`` echo, so the request's own answer is the
    only delivery evidence. It is awaited until it arrives, the turn it was
    aimed at ends, or ``STEERING_ANSWER_WAIT_SECS`` pass, whichever is first;
    the bound exists because the dashboard composer awaits this call inside a
    send request the browser aborts.

    * ``injected`` -- True is returned, which means accepted, not consumed:
      the caller keeps its pending entry for the text. The answer stays
      registered with the turn it was aimed at, whose dispatch loop proves it
      was read before the terminal (at a between-frames point with nothing
      buffered, see :meth:`_take_injected_steers`) and reports it as
      ``EVENT_STEER_CONSUMED`` only when that turn ends cleanly and this call
      has already returned True (see :meth:`_release_proven_steers`). A turn that ends on a denied approval,
      a cancel, a refusal or an error reports nothing, and the caller's
      teardown queues the pending text, so a steer codex dropped with its
      turn runs again instead of being lost. An ``injected`` answered after
      its turn ended returns False.
    * ``startedNewTurn`` -- no turn was running by the time the adapter looked,
      so it began one that no ``session/prompt`` of ours owns. That turn is
      cancelled and False is returned, which sends the caller down its queue
      path: the text runs once, as the next turn Crew starts itself.
    * ``failed``, an error answer, or an answer of any other shape -- False, and
      the caller queues it.
    * the turn ends before any answer -- False, and the caller queues it. A late
      ``startedNewTurn`` is still cancelled.

    This is at-least-once, not exactly-once. codex-acp guarantees no ordering
    between the steering answer and the prompt's terminal, and ``injected`` is
    an ack that the text entered the turn's input, not a consumption receipt.
    When the terminal is read first, or is already buffered when the answer
    is looked at, or the turn ends in a way that may have dropped the text,
    the steer is not reported consumed and the caller queues it, so a steer
    codex did act on in that window runs a second time -- visibly, as its
    own turn. That is the requeue path's documented cost
    (``_requeue_unconsumed_steers``), preferred to the silent loss the
    opposite choice produces.

    The ``is_turn_active`` and ``_prompt_written`` checks are what keep
    ``startedNewTurn`` rare: a steer is only sent once a prompt of ours has been
    written and is still in flight, so the adapter can only start a turn of its
    own when ours ends inside the request's round trip, and never while our
    prompt is still being built.

    Nothing is sent while the turn is parked on an approval. codex answers that
    request ``injected``, but its only reject option cancels the turn and drops
    what was injected with it (see ``ACP_BACKENDS_STEER``), so a steer settled
    there would be reported delivered and never run. Returning False sends it
    down the queue path instead, and it runs as the next turn. The same goes
    for a steer past ``MAX_STEERING_TEXT_CHARS`` or while
    ``MAX_STEERING_ANSWERS`` are held (the cap counts unanswered, abandoned
    and proven-but-unreported steers).
    """
    if not self.is_turn_active or not self._prompt_written or self._awaiting_permission:
        return False
    held = len(self._steering_answers) + len(self._abandoned_steering) + len(self._steers_proven)
    if len(text) > MAX_STEERING_TEXT_CHARS or held >= MAX_STEERING_ANSWERS:
        return False
    session_id = self._session_id
    aimed_at = self._prompt_starts
    turn_done = self._turn_done
    # Registered from inside the write, before its drain() can suspend: the
    # reader may resolve the answer during that suspension, and the dispatch
    # loop must already hold it when the turn's next frame arrives.
    entry: list[tuple[asyncio.Future[dict[str, Any]], int, str]] = []

    def _register(fut: "asyncio.Future[dict[str, Any]]") -> None:
        entry.append((fut, aimed_at, wrap_user_message(text)))
        self._steering_answers.append(entry[0])
        self._steering_settled[fut] = asyncio.get_running_loop().create_future()

    def _unregister() -> None:
        # Every exit that does not return True revokes the steer, wherever the
        # dispatch loop has put it: still registered, or already proven and
        # waiting for the clean-terminal report. A proven entry left behind
        # would be reported consumed to a caller that never saw True. Matched
        # by identity, so an identical text sent twice keeps the other one.
        if not entry:
            return
        if entry[0] in self._steering_answers:
            self._steering_answers.remove(entry[0])
        else:
            for i, proven in enumerate(self._steers_proven):
                if proven is entry[0][2]:
                    del self._steers_proven[i]
                    break
        self._steering_settled.pop(entry[0][0], None)

    batch_guard.note_prompt(getattr(self, "_batch_window", None), text)
    # Registered before the write, as on ``AcpClient``: a turn that ends while
    # the write is in flight finds the text in ``held_steers_possibly_delivered``.
    # It leaves once delivery is known: written and refused, never written, or
    # accepted while the turn runs.
    unresolved = getattr(self, "_held_steers_unanswered", None)
    if unresolved is None:
        unresolved = self._held_steers_unanswered = set()
    unresolved.add(text)
    params: dict[str, Any] = {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}
    if idle_prompt_required:
        params["_meta"] = {"steering": {"idleBehavior": "promptRequired"}}
    try:
        answer = await self._runtime.send_request_for_answer(
            METHOD_SESSION_STEERING,
            params,
            on_registered=_register,
        )
    except BaseException as exc:
        # The request may already be registered (``_register`` runs before the
        # write, and the write's ``drain()`` can suspend). Drop that registration
        # on every exit, cancellation included, or its answer would later settle
        # this steer as consumed while the caller, which never saw True, has
        # already moved on: the text would run with no transcript row and no
        # requeue. Unsettled, the caller's own bookkeeping queues it instead.
        if entry:
            self._runtime.forget_request(entry[0][0])
        _unregister()
        if not isinstance(exc, Exception):
            raise
        if getattr(exc, "ambiguous_delivery", False) is True:
            # The frame WAS written and sits in a pipe the live child may
            # still read: not "not written". Raised so the caller requeues
            # the steer as possibly delivered instead of as fresh text.
            raise
        unresolved.discard(text)
        logger.debug("steering request not written for %s: %s", session_id, type(exc).__name__)
        return False

    def _cancel_unowned_turn() -> None:
        # The turn the adapter started runs outside any prompt of ours, so
        # nothing would read or bound it; the caller queues the text instead.
        # ``session/cancel`` names no turn, so once a NEWER prompt of ours is
        # running it would cancel that one instead -- the user's queued turn.
        # ``_settle_abandoned_steering`` resolves these answers before the next
        # prompt starts, so this branch is a backstop, not the normal path.
        if self._prompt_starts != aimed_at and self.is_turn_active:
            logger.warning(
                "steer on %s started an adapter-owned turn after our next prompt "
                "began; not cancelling, it would hit ours",
                session_id,
            )
            return
        logger.info(
            "steer on %s started an adapter-owned turn; cancelling it and queueing",
            session_id,
        )

        async def _send_cancel() -> None:
            # Best-effort: a dead adapter has no turn left to cancel, and the
            # failure must not surface as an unhandled task exception.
            try:
                await self._runtime.send_notification(METHOD_CANCEL, {"sessionId": session_id})
            except Exception as exc:
                logger.warning(
                    "cancel of adapter-owned turn on %s failed: %s",
                    session_id,
                    type(exc).__name__,
                )

        task = asyncio.ensure_future(_send_cancel())
        self._steering_cancel_tasks.add(task)
        task.add_done_callback(self._steering_cancel_tasks.discard)

    def _late_answer(fut: "asyncio.Future[dict[str, Any]]") -> None:
        if fut.cancelled() or fut.exception() is not None:
            return
        outcome = steering_outcome(fut.result())
        if outcome == STEERING_STARTED_NEW_TURN:
            _cancel_unowned_turn()
        elif outcome == STEERING_INJECTED:
            logger.warning("steering answer on %s: injected after its turn ended", session_id)

    def _abandon() -> None:
        # Stop tracking this answer for settlement, but keep it awaited within
        # the bound so a late ``startedNewTurn`` is still cancelled and the
        # runtime's registration of it stays counted until it resolves.
        _unregister()
        if answer.done():
            # Already answered (a cancelled caller can unwind in the same loop
            # iteration the answer resolved in): act on it now, so a
            # ``startedNewTurn`` still gets its cancel.
            _late_answer(answer)
            return
        self._abandoned_steering.append(answer)

        def _release(fut: "asyncio.Future[dict[str, Any]]") -> None:
            if fut in self._abandoned_steering:
                self._abandoned_steering.remove(fut)

        answer.add_done_callback(_release)
        answer.add_done_callback(_late_answer)

    ended = asyncio.ensure_future(turn_done.wait())
    try:
        waiters: set[asyncio.Future[Any]] = {answer, ended}
        await asyncio.wait(
            waiters, timeout=STEERING_ANSWER_WAIT_SECS, return_when=asyncio.FIRST_COMPLETED
        )
    except BaseException:
        # A cancelled caller cannot settle this steer, but the runtime still
        # owes its answer, so the answer moves to the abandoned set.
        _abandon()
        raise
    finally:
        ended.cancel()
    if getattr(self._runtime, "stdin_stall_death", False) is True and not (
        answer.done() and not answer.cancelled() and answer.exception() is None
    ):
        # The frame was written, then the runtime died of a stdin stall
        # before answering: the live child may still read and inject it.
        if answer.done():
            _unregister()
        else:
            _abandon()
        raise AcpRuntimeDead(
            "runtime died of a stdin stall after the steering frame was written",
            ambiguous_delivery=True,
        )
    if not answer.done():
        # The turn ended first, or the answer outlasted the bound the caller's
        # own request can wait: the caller queues the text, and the text stays
        # in ``held_steers_possibly_delivered`` (a late ``injected`` may run it).
        _abandon()
        return False
    if answer.cancelled() or answer.exception() is not None:
        # An error answer: an adapter build without the method answers -32601.
        _unregister()
        unresolved.discard(text)
        # The class only: the error text is the adapter's, and a log line is not
        # the place to carry whatever it chose to put there.
        logger.info(
            "steering request on %s refused: %s",
            session_id,
            "cancelled" if answer.cancelled() else type(answer.exception()).__name__,
        )
        return False
    outcome = steering_outcome(answer.result())
    if outcome != STEERING_INJECTED or not (turn_done.is_set() or self._prompt_starts != aimed_at):
        # Delivery is known: refused, or accepted into the running turn. An
        # ``injected`` after its turn ended stays unresolved: codex took the text.
        unresolved.discard(text)
    if outcome == STEERING_STARTED_NEW_TURN:
        _unregister()
        _cancel_unowned_turn()
        return False
    if outcome != STEERING_INJECTED:
        _unregister()
        return False
    if turn_done.is_set() or self._prompt_starts != aimed_at:
        # Answered, but its turn is already over: nothing will report it
        # consumed. The text stays in ``held_steers_possibly_delivered``, so a
        # caller that must not run it twice keeps it delivered.
        _unregister()
        return False
    # ``injected``: accepted. The entry stays registered for this turn's
    # dispatch loop, which reports it consumed only when the turn ends
    # cleanly (see ``_release_proven_steers``); on a denial, a cancel or any
    # other ending it reports nothing, and the caller's pending entry for the
    # text is queued by the turn's teardown instead of being lost.
    self._steering_settled.pop(answer, None)
    self._steers_accepted.append(entry[0][2])
    self._last_steer_monotonic = time.monotonic()
    return True


async def _settle_abandoned_steering(self: AcpSessionHandle) -> None:
    """Resolve codex steering answers owed from the last turn before a new prompt.

    A steer whose turn ended before its answer can still be answered
    ``startedNewTurn``: the adapter then runs a turn of its own on this
    session. Its cancel is sent from the answer's callback, and
    ``session/cancel`` names no turn, so it is only safe while no prompt of
    ours is running. This runs before the next prompt starts, waits up to
    ``_STEERING_SETTLE_SECS`` for those answers, and waits for the cancels
    they schedule, so an adapter-owned turn is stopped before our prompt is
    written and none of its updates reach that prompt's queue. An answer
    still missing after the bound is forgotten and one ``session/cancel`` is
    sent in its place, which stops a turn it may already have started.
    """
    pending = [a for a in self._abandoned_steering if not a.done()]
    if pending:
        await asyncio.wait(pending, timeout=_STEERING_SETTLE_SECS)
    # Let the answers' callbacks run and schedule their cancels.
    await asyncio.sleep(0)
    unanswered = [a for a in self._abandoned_steering if not a.done()]
    for answer in unanswered:
        self._runtime.forget_request(answer)
    self._abandoned_steering = []
    if unanswered:
        logger.warning(
            "%d steering answer(s) on %s never arrived; cancelling before the next prompt",
            len(unanswered),
            self._session_id,
        )
        try:
            await self._runtime.send_notification(METHOD_CANCEL, {"sessionId": self._session_id})
        except Exception as exc:
            logger.warning(
                "pre-prompt cancel on %s failed: %s", self._session_id, type(exc).__name__
            )
    if self._steering_cancel_tasks:
        await asyncio.gather(*list(self._steering_cancel_tasks), return_exceptions=True)


def _take_injected_steers(self: AcpSessionHandle, can_settle: bool = True) -> list[str]:
    """Echo text for every codex steer this turn now knows was ``injected``.

    Called by the prompt dispatch loop between frames: after the previous
    frame is fully handled and before the next is dequeued. ``can_settle`` is
    True only while nothing is buffered on the session queue. Kiro's
    reader resolves an answer inline and routes frames into that queue
    inline, one stdout line at a time, so an answer that is done while the
    queue is empty was read after every frame already handled and before
    the terminal, which would otherwise have ended the turn. Anywhere
    else the order cannot be read, so nothing settles: the entries stay, and
    a turn that ends with them unsettled has its steers queued by the caller
    (a visible duplicate at worst, never a steer marked delivered to a turn
    that had finished). Entries aimed at an earlier prompt are dropped
    unsettled: that turn's teardown already requeued them, and settling one
    against a newer turn would mark a steer delivered that this turn never
    received. Unanswered entries stay registered.
    """
    if (
        not self._steering_answers
        or not can_settle
        or self._turn_steering_denied
        or self._cancelled
    ):
        return []
    flags = getattr(self, "_steering_settled", {})
    settled: list[str] = []
    waiting: list[tuple["asyncio.Future[dict[str, Any]]", int, str]] = []
    for answer, aimed_at, wrapped in self._steering_answers:
        flag = flags.get(answer)
        ok = False
        if aimed_at != self._prompt_starts:
            pass
        elif not answer.done():
            waiting.append((answer, aimed_at, wrapped))
            continue
        elif not answer.cancelled() and answer.exception() is None:
            ok = steering_outcome(answer.result()) == STEERING_INJECTED
        if ok:
            settled.append(wrapped)
        if flag is not None and not flag.done():
            flag.set_result(ok)
    self._steering_answers = waiting
    return settled


def _release_proven_steers(self: AcpSessionHandle, reason: str, refusal: Any) -> list[str]:
    """Echo text for this turn's proven codex steers, at a clean terminal only.

    codex's reject and ``session/cancel`` both end the turn and drop what was
    injected into it, so a steer is reported consumed only once the turn it
    entered has ended normally: no denied approval, no cancel, and a stop
    reason of exactly ``end_turn``. That is an allowlist on purpose: a bare
    wire ``refusal`` carries no ``RefusalInfo``, and error reasons such as a
    tool stall are not clean endings either. On any other ending the
    proven steers are dropped here unreported, and the caller's pending entry
    for each is queued by the turn's teardown, so the text runs again rather
    than being lost. The list is emptied either way.

    A proven steer whose ``steer()`` call has not yet returned True is not
    released either, even at a clean ``end_turn``. The caller writes the
    steer's transcript row synchronously on that return, so a report ahead
    of it would clear the pending entry before any row exists, and a
    shutdown landing in between would lose both. Held back, the entry stays
    pending: the caller resumes after the turn is done and returns False,
    and the teardown queues the text. That is the requeue path's visible
    duplicate, not a loss, and nothing on the terminal path waits on it.
    """
    proven, self._steers_proven = self._steers_proven, []
    accepted, self._steers_accepted = self._steers_accepted, []
    if self._cancelled or self._turn_steering_denied or refusal or reason != STOP_REASON_END_TURN:
        return []
    return [p for p in proven if any(p is a for a in accepted)]


def _note_steering_denial(self: AcpSessionHandle) -> None:
    """Record that an approval in this codex turn was denied.

    codex offers no per-tool reject: its reject cancels the turn and drops
    what was injected into it. Nothing in the turn is then reported consumed
    (see ``_release_proven_steers``), so every steer injected into it is
    queued by the caller. A no-op on every other backend, whose reject does
    not discard injected text.
    """
    if self._runtime.acp_backend not in ACP_BACKENDS_STEERING_REQUEST:
        return
    self._turn_steering_denied = True


async def _steer_via_goose_request(self: AcpSessionHandle, text: str) -> bool:
    """Deliver a user steer over goose's ``_goose/unstable/session/steer``.

    The shared-runtime twin of ``AcpClient._steer_via_request``. The request names
    the run it is for (``expectedRunId``, the id goose announced on
    ``session_info_update._meta.goose.activeRunId``, :meth:`_note_goose_run`), so it
    is sent only while a turn of ours is running, its prompt has been written, its
    run id is known, and no approval is open: goose's agent loop is parked on the
    approval and cannot take the text. The answer is read off the session queue
    (``send_request_for_answer``), so the turn's dispatch loop never sees it, and is
    awaited until it arrives, the turn ends, or ``STEERING_ANSWER_WAIT_SECS`` pass.

    True means goose queued the text for the running turn, not that the turn read
    it. goose echoes each steer it drains as a ``user_message_chunk`` carrying
    ``_meta.goose.steer`` and the answer's ``messageId``, which
    :meth:`_goose_steer_echo` reports as ``EVENT_STEER_CONSUMED``. A steer still
    queued when its run ends is discarded by goose and never echoed, so the
    caller's pending entry is queued by the turn's teardown
    (``steer_needs_loss_recovery``). False -- an error answer (an older goose
    answers ``-32601``), no answer in time, or no run to aim at -- sends the caller
    down its queue path.
    """
    run_id = self._goose_active_run_id
    if (
        not self.is_turn_active
        or not self._prompt_written
        or self._awaiting_permission
        or not run_id
        or len(text) > MAX_STEERING_TEXT_CHARS
        or len(self._goose_steers) >= MAX_STEERING_ANSWERS
    ):
        return False
    wrapped = wrap_user_message(text)
    entry: list[tuple[asyncio.Future[dict[str, Any]], str]] = []

    def _register(fut: "asyncio.Future[dict[str, Any]]") -> None:
        # Before the write, so an echo read before this call resumes still
        # finds the answer to match (:meth:`_goose_steer_echo`).
        entry.append((fut, wrapped))
        self._goose_steers.append(entry[0])

    def _forget() -> None:
        if entry and entry[0] in self._goose_steers:
            self._goose_steers.remove(entry[0])

    batch_guard.note_prompt(getattr(self, "_batch_window", None), text)
    turn_done = self._turn_done
    try:
        answer = await self._runtime.send_request_for_answer(
            METHOD_GOOSE_SESSION_STEER,
            {
                "sessionId": self._session_id,
                "expectedRunId": run_id,
                "prompt": [{"type": "text", "text": text}],
            },
            on_registered=_register,
        )
    except BaseException as exc:
        if entry:
            self._runtime.forget_request(entry[0][0])
        _forget()
        if not isinstance(exc, Exception):
            raise
        if getattr(exc, "ambiguous_delivery", False) is True:
            raise
        logger.debug("goose steer not written for %s: %s", self._session_id, type(exc).__name__)
        return False
    ended: asyncio.Future[Any] = asyncio.ensure_future(turn_done.wait())
    waiters: set[asyncio.Future[Any]] = {answer, ended}
    try:
        await asyncio.wait(
            waiters, timeout=STEERING_ANSWER_WAIT_SECS, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        ended.cancel()
    message_id = _goose_message_id(answer)
    if not message_id:
        # The class only: the error text is the harness's.
        logger.info(
            "goose steer on %s not accepted: %s",
            self._session_id,
            "no answer" if not answer.done() else "refused",
        )
        if not answer.done():
            self._runtime.forget_request(answer)
        _forget()
        return False
    self._last_steer_monotonic = time.monotonic()
    return True


def _goose_message_id(answer: "asyncio.Future[dict[str, Any]]") -> str:
    """The ``messageId`` an accepted goose steer answer carries, or ``""``."""
    if not answer.done() or answer.cancelled() or answer.exception() is not None:
        return ""
    result = answer.result()
    message_id = result.get("messageId") if isinstance(result, dict) else None
    return message_id if isinstance(message_id, str) else ""


def _note_goose_run(self: AcpSessionHandle, msg: JsonRpcMessage) -> None:
    """Track the run id goose announces for this session's running prompt
    (``goose_announced_run``, the reader ``AcpClient`` uses too)."""
    announced, run_id = goose_announced_run(msg.params)
    if announced:
        self._goose_active_run_id = run_id


def _goose_steer_echo(self: AcpSessionHandle, msg: JsonRpcMessage) -> AcpEvent | None:
    """``EVENT_STEER_CONSUMED`` for goose's echo of a steer this session sent.

    Matched by the ``messageId`` the steer's answer returned (``goose_steer_echo_id``,
    the reader ``AcpClient`` uses too), never by text. A steer spanning several
    content blocks echoes once per block; the first settles it.
    """
    if not self._goose_steers:
        return None
    echoed = goose_steer_echo_id(msg.params)
    if not echoed:
        return None
    for held in self._goose_steers:
        if _goose_message_id(held[0]) == echoed:
            self._goose_steers.remove(held)
            return AcpEvent(kind=EVENT_STEER_CONSUMED, text=held[1])
    return None
