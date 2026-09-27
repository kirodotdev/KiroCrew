"""Meeting session state — per-agent batching dispatcher and lifecycle.

A live meeting fans transcription lines out to several background agent
sessions, each maintaining its own output file (notes, diagram, task list). The
per-agent :class:`AgentQueue` batches lines so an agent gets a paragraph of
context every ~30s instead of one interruption per utterance, and a circuit
breaker pauses an agent whose dispatches keep failing.

**Dispatch is in-process.** Upstream posted every batch back to its own gateway
over authenticated loopback HTTP (``POST /api/chat`` with an internal secret).
Here the app's routes are registered ON the gateway, so a batch goes straight to
the shared :class:`~kiro_crew.session.SessionManager` via
:func:`~kiro_crew.llm_helpers.stream_and_collect` — no socket, no secret, no
second copy of the auth path. Approval runs under
:data:`~kiro_crew.llm_helpers.ToolApprovalPolicy.HOOK_BASED` so the agents'
file writes still traverse the PreToolUse gate (deny patterns, sensitive paths,
governance) exactly like any other turn.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain.dictionary import DomainDictionary
from kiro_crew.apps.builtins.meetings.backend.domain.translate import (
    TranslationQueue,
    run_oneshot_translation,
)
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.security import redact
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.meetings")

#: Separator between queued transcript lines in one dispatched batch. A blank
#: line reads as a paragraph break to the agent, so fragments of speech stay
#: visually distinct rather than running together into one sentence.
_BATCH_SEP = "\n\n"

#: Ceiling on batches one `flush_now()` drain will dispatch. A meeting that
#: accumulated a huge backlog still ends in bounded time, and a dispatch that
#: keeps failing cannot spin the loop. Generous: at MAX_BATCH_CHARS each, this
#: is far more transcript than a real meeting produces.
_MAX_DRAIN_BATCHES = 50

# Once its session has been acquired, one kickoff turn must only establish the
# agent's output contract. A harness that interprets "wait for transcription"
# as an instruction to hold the turn can otherwise keep the meeting in its
# bounded initialization hold forever.
_AGENT_INIT_TIMEOUT_SECS = 120.0
# Stop may wait on an ordinary transcript turn already in flight, so that explicit
# teardown needs a ceiling. Ordinary batching and other lifecycle drains stay
# unbounded: there is no measured duration that makes a healthy turn disposable,
# and those paths have no recovery response that can direct the user to Reset.
_AGENT_DRAIN_TIMEOUT_SECS = 120.0
# Give ACP a short cooperative-cancel window after any turn budget expires. The
# meeting continues even if the harness does not acknowledge cancellation.
_AGENT_CANCEL_ACK_SECS = 5.0

# The process-wide dictionary, reloaded whenever the user edits it.
_dictionary = DomainDictionary()


#: Serializes every access to the process-wide shared dictionary.
#:
#: Lives HERE, with the state it guards, rather than at one of its callers: the
#: object is module-global, so a lock owned by `routes/settings.py` covered the two
#: mutating routes and left `reload_dictionary`'s other callers (the GET, the
#: explicit reload, the startup counter) racing them. A read that lands mid-edit
#: resets the shared object before the edit is saved, so a successful add
#: disappears.
#:
#: An RLock, for the same reason as `store._META_LOCK`: `_add_term` holds it across
#: a reload-mutate-save, and `reload_dictionary` takes it too.
_DICTIONARY_LOCK = threading.RLock()


def dictionary_transaction() -> "threading.RLock":
    """The lock guarding the shared dictionary. Use as ``with``.

    Callers that reload, MUTATE and save need it across all three; a bare
    :func:`reload_dictionary` takes it internally.
    """
    return _DICTIONARY_LOCK


def reload_dictionary(root: Path | None = None) -> DomainDictionary:
    """(Re)load the shared dictionary from disk and return it.

    Takes :func:`dictionary_transaction` itself, so a plain read cannot land in the
    middle of another request's reload-mutate-save and reset the object before the
    save.
    """
    with _DICTIONARY_LOCK:
        _dictionary.load(store.dictionary_path(root))
        return _dictionary


def shared_dictionary() -> DomainDictionary:
    return _dictionary


def slot_key(agent_id: str, meeting_id: str) -> str:
    """Session key for one agent's stream in one meeting."""
    return f"{k.SLOT_PREFIX}-{agent_id}-{meeting_id}"


#: Fragments the recognizer emits on its own, which carry no meeting content.
#:
#: An explicit set rather than a shape rule. The old rule — "three or fewer words,
#: all one or two characters" — was measured against the noise it was written for
#: and not against real speech, so it also dropped meaningful short utterances:
#: ``"I do"``, ``"we go"``, ``"no it is"``, ``"do it"``, ``"he is up"``. Those are
#: the answers to questions, and losing them removes exactly the decision a meeting
#: was held to reach, with nothing in the notes to show a turn was dropped.
#:
#: Enumerating instead means a fragment not on the list reaches the agents. That is
#: the right direction to fail: a stray ``"uh"`` in the transcript costs a reader
#: nothing, while a missing ``"I do"`` can invert the meaning of a decision.
_NOISE_FRAGMENTS = frozenset(
    {
        "",
        "i",
        "uh",
        "um",
        "ah",
        "eh",
        "oh",
        "hm",
        "hmm",
        "mm",
        "mhm",
        "er",
        "erm",
        "ok",
        "okay",
        "so",
        "and",
        "but",
        "the",
        "a",
    }
)


def is_noise(text: str) -> bool:
    """True for transcription fragments not worth an agent turn.

    A segment is noise only when EVERY word is a recognizer filler
    (:data:`_NOISE_FRAGMENTS`) — so ``"uh"``, ``"I I"`` and ``"OK so uh"`` are
    dropped while ``"I do"``, ``"we go"`` and ``"no it is"`` are not, because
    ``do``/``go``/``no``/``it``/``is`` are real words that happen to be short.

    Length-capped as well: a long run of fillers is still filler, but a segment
    with many words is far more likely to be speech the filter should not judge.
    """
    words = text.lower().split()
    if not words or len(words) > 6:
        return False
    return all(word.strip(".,!?;:") in _NOISE_FRAGMENTS for word in words)


# ── agent dispatch ──────────────────────────────────────────────────────────


async def dispatch_to_agent(
    sessions: Any,
    key: str,
    text: str,
    agent: str = "",
    *,
    hooks: Any = None,
    timeout_secs: float | None = None,
) -> None:
    """Send one batch to an agent's background session.

    Raises on failure so :class:`AgentQueue`'s circuit breaker can see it.
    """
    if sessions is None:
        raise RuntimeError("session manager unavailable")
    # Session acquisition is deliberately outside the kickoff turn timeout. If a
    # cold start were cancelled here, the later transcript batches would inherit a
    # session that never received its OUTPUT_FILE contract. The timeout begins only
    # once there is a provider that can receive the kickoff.
    provider, _is_new, _resumed = await sessions.get_or_create(key, agent=agent or None)

    async def _run_turn() -> None:
        # Identity is threaded so the PreToolUse gate resolves ceiling ∩ PROFILE,
        # not the ceiling alone. The gate can only look up a profile whose name it
        # was given, and with these empty an operator profile narrowing this app —
        # denying `filesystem.write`, say — was silently not applied to tools this
        # dispatch approved. `app` is the load-bearing one; `session_key`/`agent`
        # additionally make the SEL audit attribute the call to this meeting rather
        # than to an anonymous background turn.
        await stream_and_collect(
            provider,
            text,
            approval_policy=ToolApprovalPolicy.HOOK_BASED,
            hooks=hooks,
            session_key=key,
            agent=agent,
            app=k.APP_NAME,
        )

    turn_task = asyncio.create_task(_run_turn())
    try:
        if timeout_secs is None:
            await asyncio.shield(turn_task)
        else:
            # Keep consuming ACP's terminal response while cancel() waits for it.
            # Unwinding the consumer first marks the turn done and makes cancel()
            # return no_turn without sending the native session/cancel.
            await asyncio.wait_for(asyncio.shield(turn_task), timeout=timeout_secs)
    except asyncio.TimeoutError:
        if timeout_secs is None:
            # A provider-originated TimeoutError on an ordinary turn is a regular
            # dispatch failure. Only our explicit lifecycle clock gets the
            # pause-after-one teardown treatment below.
            raise
        try:
            await provider.cancel(wait_ack_timeout=_AGENT_CANCEL_ACK_SECS)
        except Exception:
            logger.debug(
                "meetings: timed-out agent cancellation failed for %s",
                key,
                exc_info=True,
            )
        raise asyncio.TimeoutError from None
    except asyncio.CancelledError:
        # A lifecycle drain can cancel an ordinary, otherwise-unbounded turn after
        # its own wait budget. Keep the consumer alive until native cancellation is
        # requested, just like the explicit timeout path above.
        try:
            await provider.cancel(wait_ack_timeout=_AGENT_CANCEL_ACK_SECS)
        except Exception:
            logger.debug(
                "meetings: cancelled agent turn cleanup failed for %s",
                key,
                exc_info=True,
            )
        raise
    finally:
        # After cooperative cancellation's acknowledgement window, retire any
        # remaining consumer before releasing its turn lease. Also clean up the
        # shielded task if the caller itself was cancelled.
        if not turn_task.done():
            turn_task.cancel()
        await asyncio.gather(turn_task, return_exceptions=True)
        # The session is long-lived for the meeting's duration (each batch adds to
        # the same conversation), so release the turn semaphore but never destroy.
        try:
            sessions.release(key)
        except Exception:
            logger.debug("meetings: session release failed for %s", key, exc_info=True)


@dataclass
class AgentQueue:
    """Per-agent message queue with time-based batching + a circuit breaker."""

    name: str
    key: str
    agent: str = ""
    sessions: Any = None
    hooks: Any = None
    queue: list[str] = field(default_factory=list)
    busy: bool = False
    batch_interval: float = k.BATCH_INTERVAL_SECS
    _flush_task: asyncio.Task | None = field(default=None, repr=False)
    _drain_task: asyncio.Task | None = field(default=None, repr=False)
    _drain_requested: bool = field(default=False, repr=False)
    _flush_soon_requested: bool = field(default=False, repr=False)
    _drain_incomplete: bool = field(default=False, repr=False)
    _fail_count: int = 0
    _backoff: float = 0.0

    @property
    def fail_count(self) -> int:
        return self._fail_count

    @property
    def paused(self) -> bool:
        """True when repeated dispatch failures tripped the breaker."""
        return self._fail_count >= k.MAX_DISPATCH_FAILURES

    @property
    def drain_incomplete(self) -> bool:
        """Whether an explicit Stop drain left queued agent work behind."""
        return self._drain_incomplete

    def enqueue(self, text: str) -> None:
        self.queue.append(text)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return  # a timer is already running
        try:
            self._flush_task = asyncio.get_running_loop().create_task(self._delayed_flush())
        except RuntimeError:
            # No running loop (a sync test constructing a queue). The next
            # enqueue on-loop, or an explicit flush_now(), does the work.
            self._flush_task = None

    def flush_soon(self) -> None:
        """Schedule queued speech for the next event-loop turn.

        Used after initialization so a meeting's opening does not wait through
        the ordinary batch interval. This stays synchronous: lifecycle callers
        can release their locks without awaiting an unbounded agent turn.
        """
        task = self._flush_task
        if task is not None and not task.done():
            if self.busy:
                # The timer is already inside a live turn, so it cannot be
                # replaced. Remember the request and skip the NEXT batch delay
                # after that turn completes; otherwise opening speech enqueued
                # during initialization can still wait the ordinary 30 seconds.
                self._flush_soon_requested = True
                return
            task.cancel()  # replace the sleeping batch timer
        try:
            self._flush_task = asyncio.get_running_loop().create_task(
                self._delayed_flush(first_delay=0.0)
            )
        except RuntimeError:
            self._flush_task = None

    async def _delayed_flush(self, first_delay: float | None = None) -> None:
        """The batching timer: sleep, flush, and keep going while work remains.

        The loop lives HERE rather than as a ``_schedule_flush()`` call inside
        ``flush()``, because this coroutine IS the body of ``_flush_task`` — so from
        inside it ``self._flush_task.done()`` is False and ``_schedule_flush`` takes
        its "a timer is already running" early return. A reschedule attempted from
        within ``flush()`` therefore does nothing at all, which is what let a queue
        needing a second batch stall until teardown discarded its tail.

        Bounded by :data:`_MAX_DRAIN_BATCHES` so neither a large backlog nor a
        persistently failing dispatch can keep one task alive indefinitely; the
        circuit breaker (``paused``) is the other exit.
        """
        delay = first_delay
        for _ in range(_MAX_DRAIN_BATCHES):
            await asyncio.sleep((self.batch_interval if delay is None else delay) + self._backoff)
            delay = None
            if not await self.flush():
                return
            if self._drain_requested:
                # Let flush_now() own the rest of the immediate drain after the live
                # turn it was waiting for completes.
                return
            if self._flush_soon_requested:
                self._flush_soon_requested = False
                delay = 0.0

    async def flush_now(self, timeout_secs: float | None = None) -> bool:
        """Force an immediate flush (meeting end / pause).

        Return ``False`` when queued work remains after the drain.  An explicit
        ``timeout_secs`` caller also installs the Reset recovery guard, whether
        the bounded drain ran out of time or the circuit breaker paused it first.

        Every caller joins one queue-owned drain task. This matters for the direct
        agent-message endpoint: its drain is not ``_flush_task``, so ``busy`` alone
        cannot identify the coroutine that Stop must await. Sharing the complete
        drain makes Stop wait for that direct turn and any lines queued behind it
        before teardown can clear the session.

        The timeout wraps the complete shared drain. Cancelling that task reaches
        ``dispatch_to_agent`` while its stream consumer is still alive, so its
        native ACP cancellation path runs and the undispatched batch stays queued.
        """
        task = self._drain_task
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(self._drain())
            self._drain_task = task
        try:
            if timeout_secs is None:
                return await asyncio.shield(task)
            drained = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_secs)
            if not drained:
                self._pause_after_incomplete_drain("the circuit breaker paused")
            return drained
        except asyncio.CancelledError:
            # Another waiter with an explicit lifecycle budget can cancel the
            # shared drain. Its unbounded peers must observe the same incomplete
            # result instead of leaking CancelledError through an unrelated HTTP
            # request. A cancellation of THIS caller leaves the shielded drain
            # running, so the inner task is not cancelled and we propagate it.
            if task.cancelled():
                return False
            raise
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._pause_after_incomplete_drain("the turn budget expired")
            return False
        finally:
            if self._drain_task is task and task.done():
                self._drain_task = None

    async def _drain(self) -> bool:
        """Drain this queue once for all concurrent ``flush_now`` callers.

        A pending ``_flush_task`` is in one of two states, and they must be treated
        differently. Still SLEEPING on its batch interval: cancelling it is the
        point — we are flushing now instead of later. Already inside ``flush()`` and
        awaiting the agent: cancelling **kills the live turn**, and because
        ``self.busy`` is still set the follow-up ``flush()`` below then returns
        immediately — so ending a meeting mid-dispatch lost that batch AND the
        finalization notice, which is the one moment a meeting's notes matter most.

        ``self.busy`` is the discriminator, and it is only true between entering
        ``flush()`` and its ``finally``, so an in-flight dispatch is awaited to
        completion rather than interrupted. Awaiting the task (not just the flag)
        means a dispatch that fails still runs its except-branch bookkeeping.
        """
        task = self._flush_task
        if task is not None and not task.done():
            if self.busy:
                # Mid-dispatch: let it finish. Its own `finally` clears `busy`, and a
                # failure inside it is already handled by `flush()`'s except-branch,
                # so nothing needs to propagate out of the drain.
                self._drain_requested = True
                try:
                    await task
                except Exception:
                    logger.debug(
                        "meetings: in-flight flush for %s ended in error",
                        self.name,
                        exc_info=True,
                    )
                finally:
                    self._drain_requested = False
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        # Drain, not flush-once. A queue over MAX_BATCH_CHARS takes several batches,
        # and at pause/stop there is no later timer to finish the job — whatever is
        # still queued when this returns is discarded by teardown. Bounded by
        # _MAX_DRAIN_BATCHES so a producer that keeps enqueuing cannot spin here
        # forever. Dispatch failures have the independent circuit-breaker bound:
        # `flush()` returns False once it pauses the queue. Queue length is not a
        # progress signal because a line can arrive while one same-sized batch is
        # consumed, leaving the length unchanged even though the drain advanced.
        for _ in range(_MAX_DRAIN_BATCHES):
            if not self.queue:
                break
            if not await self.flush():
                break
        if self.queue:
            logger.warning(
                "meetings: %s still has %d queued line(s) after a full drain",
                self.name,
                len(self.queue),
            )
        return not self.queue

    def _pause_after_incomplete_drain(self, reason: str) -> None:
        self._drain_incomplete = True
        self._fail_count = k.MAX_DISPATCH_FAILURES
        self._backoff = k.BACKOFF_CAP_SECS
        logger.error(
            "meetings: teardown drain for %s did not finish because %s; "
            "queue paused with %d line(s)",
            self.name,
            reason,
            len(self.queue),
        )

    def resume(self) -> None:
        """Reset the breaker and retry whatever is queued."""
        self._fail_count = 0
        self._backoff = 0.0
        if self.queue:
            self._schedule_flush()

    async def recover_incomplete_drain(self, timeout_secs: float) -> bool:
        """Retry a retained Stop batch before clearing its recovery guard.

        ``resume()`` resets the circuit breaker, but an incomplete drain remains a
        data-retention boundary until the queued transcript is actually delivered.
        Clearing the marker when Reset merely SCHEDULED a retry let an immediate
        Stop tear the session down while that retry was still pending.
        """
        if not self._drain_incomplete:
            return True
        drained = await self.flush_now(timeout_secs=timeout_secs)
        recovered = drained and not self.queue
        if recovered:
            self._drain_incomplete = False
        return recovered

    def _take_batch(self) -> tuple[str, int]:
        """The next batch and HOW MANY queued lines it consumed.

        Whole lines only, up to ``k.MAX_BATCH_CHARS``. The count is the contract:
        the caller deletes exactly the lines that were dispatched, so a queue over
        the cap carries its tail into the next flush instead of losing it.

        Truncating the joined string and then clearing the whole queue silently
        DESTROYED transcript — the visible symptom is a long pause (which lets the
        queue exceed 60k) followed by notes that skip the end of what was said.
        A single line longer than the cap is still truncated and consumed, because
        keeping it would wedge the queue forever.
        """
        lines: list[str] = []
        used = 0
        for line in self.queue:
            # Plus the separator this line will need — every line but the first.
            cost = len(line) + (len(_BATCH_SEP) if lines else 0)
            if lines and used + cost > k.MAX_BATCH_CHARS:
                break
            lines.append(line)
            used += cost
        return _BATCH_SEP.join(lines)[: k.MAX_BATCH_CHARS], len(lines)

    async def flush(self) -> bool:
        """Dispatch one batch. Returns True when more work is still queued.

        The return value is the signal ``_delayed_flush`` and ``flush_now`` loop on:
        a queue over ``MAX_BATCH_CHARS`` needs several batches, and this call
        deliberately sends exactly one so a single turn stays bounded.
        """
        if not self.queue or self.busy or self.paused:
            return False
        batch, size = self._take_batch()
        self.busy = True
        more_queued = False
        try:
            await dispatch_to_agent(
                self.sessions,
                self.key,
                batch,
                self.agent,
                hooks=self.hooks,
            )
            del self.queue[:size]
            self._fail_count = 0
            self._backoff = 0.0
            # Lines that arrived during the dispatch, OR the tail of a queue that
            # exceeded MAX_BATCH_CHARS and so needs more than one batch.
            more_queued = bool(self.queue)
        except Exception as exc:
            self._fail_count += 1
            self._backoff = min(k.BACKOFF_STEP_SECS * self._fail_count, k.BACKOFF_CAP_SECS)
            logger.error(
                "meetings: dispatch to %s failed (%d/%d), backoff %.0fs: %s",
                self.name,
                self._fail_count,
                k.MAX_DISPATCH_FAILURES,
                self._backoff,
                exc,
            )
            more_queued = not self.paused
        finally:
            self.busy = False
        return more_queued

    def cancel(self) -> None:
        """Drop any pending timer (meeting teardown)."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
        self._drain_task = None


# ── config helpers ──────────────────────────────────────────────────────────


def get_enabled_agents(
    config: dict[str, Any], agents_enabled: list[str] | None = None
) -> list[dict[str, Any]]:
    """Agent definitions filtered by an explicit allow-list, else by defaults."""
    all_agents = config.get("meeting_agents") or []
    if agents_enabled is not None:
        allowed = set(agents_enabled)
        return [a for a in all_agents if a.get("id") in allowed]
    return [a for a in all_agents if a.get("enabled_by_default", True)]


# ── the live session ────────────────────────────────────────────────────────


@dataclass
class MeetingSession:
    """Tracks one active meeting's agent queues and mute state."""

    meeting_id: str
    sessions: Any = None
    hooks: Any = None
    agents_enabled: list[str] | None = None
    config: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    agents: dict[str, AgentQueue] = field(default_factory=dict)
    muted_agents: set[str] = field(default_factory=set)
    #: Lines held while THIS session's agents initialize, in arrival order, each
    #: paired with the agent names an unmuted fan-out would have reached AT THE
    #: MOMENT IT WAS SPOKEN.
    #:
    #: Lives on the session, not on the holder, so it is bound to the identity
    #: whose initialization it covers: a session that is replaced or torn down
    #: takes its hold with it, and a later session can never inherit and replay
    #: lines that were spoken into a meeting that does not exist.
    #:
    #: The recipient set is stored rather than recomputed at drain because the
    #: hold must change WHEN a line is delivered, never WHO it was addressed to.
    #: Resolving it at drain let a mute applied during initialization reach
    #: backwards and rob a line spoken while that agent was still listening —
    #: an outcome the live path cannot produce, since it fans out immediately.
    #:
    #: NAMES, not queue objects: an agent disabled mid-initialization has its
    #: queue removed from ``agents``, and holding a reference would enqueue into
    #: a queue nothing flushes. A name that does not resolve is simply skipped.
    init_buffer: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    #: How many of the OLDEST held lines the cap displaced. Read at drain time
    #: to size the marker, so a drop is announced once with an exact count
    #: rather than per line.
    init_dropped: int = 0
    #: The agents that lost one of those displaced lines — the union of the
    #: recipient sets recorded on every line the cap dropped.
    #:
    #: The marker's audience has to be derived from the DROPPED lines, not from
    #: whoever is unmuted at drain time. Those two sets diverge the moment a mute
    #: lands between the drop and the drain: the agent still receives the
    #: surviving pre-mute lines (they carry their own arrival-time recipients) but
    #: would fall outside a drain-time audience, so its output would begin
    #: mid-conversation with nothing to say a turn was lost — the exact silent gap
    #: the marker exists to prevent.
    init_dropped_recipients: set[str] = field(default_factory=set)
    #: Data root override, threaded through so the translation worker's writes land
    #: in the test tmp dir rather than the real app data dir.
    root: Any = None
    #: Live transcript translation, or None when no target language is configured
    #: (the default). Not an ``AgentQueue``: see ``domain/translate.py``.
    translations: "TranslationQueue | None" = field(default=None, init=False)
    #: Set once, the first time this session's transcript ingress is opened
    #: (``_ActiveMeeting.resume_dispatches``) — i.e. it finished agent init and
    #: became genuinely usable. Monotonic: never cleared, because it records that
    #: the meeting REACHED the ready state, not that it is ready right now
    #: (ingress toggles off on every suspend). ``abandoned`` reads it to tell a
    #: meeting retired mid-init (never ready → terminal) apart from an
    #: established meeting whose idle slots were reaped but resume on the next
    #: line (was ready → recoverable, must NOT be treated as abandoned).
    became_ready: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        config = self.config if self.config is not None else store.read_config()
        enabled = get_enabled_agents(config, self.agents_enabled)
        for agent_def in enabled:
            agent_id = str(agent_def.get("id") or "")
            if not agent_id:
                continue
            self.agents[agent_id] = self._make_queue(agent_id, agent_def.get("agent") or "")
        # The task extractor always runs — it is the app's core output, not a
        # configurable agent.
        self.agents[k.TASK_EXTRACTOR_ID] = self._make_queue(
            k.TASK_EXTRACTOR_ID, k.TASK_EXTRACTOR_AGENT
        )
        # Live translation, only when a target language is configured. Built here
        # rather than lazily so the language is fixed for the meeting: changing it
        # mid-flight would interleave two languages in one panel.
        language = str(config.get("translation_language") or "")
        if language in k.TRANSLATION_LANG_CODES and self.sessions is not None:
            sessions = self.sessions
            self.translations = TranslationQueue(
                meeting_id=self.meeting_id,
                language=language,
                runner=lambda prompt: run_oneshot_translation(sessions, prompt),
                root=self.root,
            )

    def _make_queue(self, agent_id: str, agent: str) -> AgentQueue:
        return AgentQueue(
            name=agent_id,
            key=slot_key(agent_id, self.meeting_id),
            agent=agent,
            sessions=self.sessions,
            hooks=self.hooks,
        )

    def add_agent(self, agent_id: str, agent: str) -> AgentQueue:
        """Enable an agent mid-meeting (idempotent)."""
        queue = self.agents.get(agent_id)
        if queue is None:
            queue = self._make_queue(agent_id, agent)
            self.agents[agent_id] = queue
        self.muted_agents.discard(agent_id)
        return queue

    def _prepare_line(self, text: str) -> str:
        """Correct and clamp one inbound line; ``""`` when it is not worth a turn.

        The front half of :meth:`broadcast`, split out so the initialization hold
        can run it at ARRIVAL rather than at delivery. Deferring it made the hold's
        cap dishonest: recognizer filler occupied a slot until drain and only then
        was discarded, so a burst of ``"uh"`` could evict the genuine opening
        speech it was supposed to be counted against.
        """
        text = _dictionary.correct(text.strip())[: k.MAX_TRANSCRIPT_CHARS]
        return "" if not text or is_noise(text) else text

    def _recipient_names(self) -> frozenset[str]:
        """The agents an unmuted fan-out would reach right now."""
        return frozenset(
            queue.name for queue in self.agents.values() if queue.name not in self.muted_agents
        )

    def broadcast(self, text: str) -> int:
        """Correct, filter, and enqueue *text* for every unmuted agent.

        Returns the number of queues that accepted the line (0 when filtered).
        """
        prepared = self._prepare_line(text)
        if not prepared:
            return 0
        # Translated from the DICTIONARY-CORRECTED text, and from inside the same
        # noise gate the agents get: the corrections exist because speech-to-text
        # mangles project nouns, and a mangled noun mistranslates into something
        # unrecognisable. The CHAT_PREFIX marker is agent context, not speech, so
        # translating it would spend prompt tokens on it and surface the literal
        # marker in the panel's source column. It is stripped from the RAW line,
        # before correction and the length cap, so a max-length typed message is
        # capped on its payload rather than losing its tail to the marker's
        # characters. Enqueueing never blocks or raises, and the count below
        # deliberately does not include it — `dispatched` means "agents reached".
        if self.translations is not None:
            source = prepared
            raw = text.strip()
            if raw.startswith(k.CHAT_PREFIX):
                rest = raw[len(k.CHAT_PREFIX) :]
                # Anchored to a word boundary: only the bare marker or "[chat] …"
                # is agent context. Speech that merely starts with a marker-like
                # token (e.g. "[chat]room …") is kept verbatim.
                if not rest or rest[:1].isspace():
                    source = self._prepare_line(rest.lstrip())
            self.translations.enqueue(source)
        accepted = 0
        for name in self._recipient_names():
            self.agents[name].enqueue(prepared)
            accepted += 1
        return accepted

    def buffer_during_init(self, text: str) -> bool:
        """Hold *text* until initialization finishes. False when it displaced a line.

        The agents cannot receive anything yet — they do not know which file they
        own until ``init_agents`` has run — but the speaker is already talking, and
        refusing the line loses the opening of the meeting.

        Normalized and filtered HERE, at arrival, and stored with the recipients of
        this moment: both halves of "what happens to this line" are decided when a
        live line would have decided them, so the hold only ever shifts delivery in
        TIME. Filler never occupies a slot it would later vacate, and a mute landing
        mid-initialization cannot reach back to a line spoken before it.

        Bounded by :data:`~..constants.MAX_INIT_BUFFER_LINES`, and the overflow
        rule is DROP-OLDEST: an initialization slow enough to overflow is one where
        the newest speech is the most likely to still be in play, and the tail is
        what the agents need to pick up a conversation mid-flight. The count of
        displaced lines is kept so :meth:`drain_init_buffer` can announce them —
        dropping them quietly is the one outcome worse than refusing the request.

        Returns whether the hold is intact: a filtered line displaces nothing, so
        it reports True alongside a line that simply fit.
        """
        prepared = self._prepare_line(text)
        if not prepared:
            return True
        self.init_buffer.append((prepared, self._recipient_names()))
        overflow = len(self.init_buffer) - k.MAX_INIT_BUFFER_LINES
        if overflow <= 0:
            return True
        for _line, recipients in self.init_buffer[:overflow]:
            self.init_dropped_recipients |= recipients
        del self.init_buffer[:overflow]
        self.init_dropped += overflow
        return False

    def drain_init_buffer(self) -> tuple[int, int]:
        """Fan every held line out in ARRIVAL ORDER. Returns ``(delivered, dropped)``.

        Called once, immediately after initialization completes and under the same
        ``DISPATCH_LOCK`` acquisition that reopens ingress — so a live line arriving
        the instant ingress opens cannot overtake the held ones and reorder the
        meeting's opening.

        Each line is replayed to the recipients recorded when it was SPOKEN, not to
        whoever is unmuted now: correction, filtering and addressing were all decided
        at arrival, so the hold shifts delivery in time and nothing else. A recorded
        agent whose queue is gone (disabled mid-initialization) is skipped rather
        than resurrected.

        A drop is announced FIRST, because drop-oldest puts the gap at the head of
        what survived — so the marker sits where the missing turns actually were, and
        it is addressed to the agents that LOST one of the dropped lines rather than
        to whoever happens to be unmuted at drain time. Those two audiences diverge
        under a mute landing mid-initialization, and the agent with the gap is
        precisely the one a drain-time audience would omit.
        """
        held, dropped = self.init_buffer, self.init_dropped
        deprived = self.init_dropped_recipients
        self.init_buffer, self.init_dropped = [], 0
        self.init_dropped_recipients = set()
        if dropped:
            marker = k.SYSTEM_INIT_BUFFER_OVERFLOW.format(
                count=dropped, limit=k.MAX_INIT_BUFFER_LINES
            )
            for name in deprived:
                queue = self.agents.get(name)
                if queue is not None:
                    queue.enqueue(marker)
        delivered = 0
        for line, recipients in held:
            reached = [self.agents[name] for name in recipients if name in self.agents]
            for queue in reached:
                queue.enqueue(line)
            if reached:
                delivered += 1
        return delivered, dropped

    @property
    def expired(self) -> bool:
        return (time.time() - self.started_at) > k.MAX_SESSION_DURATION

    @property
    def abandoned(self) -> bool:
        """Whether this meeting was retired mid-init and never became usable.

        The case this guards: a gateway-wide session sweep (the dashboard's "Kiro
        identity changed" reconcile) retires this meeting's agent sessions while
        it is still initializing, so it holds the single-active-meeting latch
        with no live slot and never reaches dispatch-ready. Because it is young,
        :attr:`expired` stays False, so without this signal it wedges the latch
        as ``status: active`` forever.

        Two conditions, BOTH required:

        * ``not became_ready`` — the meeting never finished init (ingress was
          never opened). This is what excludes the healthy case an idle sweep
          creates: an ESTABLISHED meeting that goes quiet past the idle timeout
          has its agent slots reaped from the session registry too (they are not
          persistent/channel-exempt), making every ``has_session`` read False —
          but it already became ready, its slots were reaped with the resume SID
          preserved, and its next line resumes them via ``get_or_create``. That
          meeting is recoverable and must NOT read as abandoned.
        * every installed agent slot is gone from the registry — no live session
          resolves for any ``slot_key``.

        Returns False when there is no session manager or no installed slot to
        judge, so expiry/teardown stay in charge and it never fires spuriously.
        """
        if self.became_ready:
            return False
        if self.sessions is None or not self.agents:
            return False
        return not any(self.sessions.has_session(queue.key) for queue in self.agents.values())

    @property
    def agents_paused(self) -> bool:
        return any(queue.paused for queue in self.agents.values())

    @property
    def agent_drain_incomplete(self) -> bool:
        return any(queue.drain_incomplete for queue in self.agents.values())

    def status(self) -> dict[str, Any]:
        return {
            "active_meeting": self.meeting_id,
            "muted_agents": sorted(self.muted_agents),
            "agents": {
                name: {
                    "busy": queue.busy,
                    "queued": len(queue.queue),
                    "fail_count": queue.fail_count,
                    "paused": queue.paused,
                }
                for name, queue in self.agents.items()
            },
            "agents_paused": self.agents_paused,
            "expired": self.expired,
        }

    async def flush_all(self, *, timeout_secs: float | None = None) -> bool:
        """Drain every agent, sharing one optional wall-clock budget per queue."""
        results = await asyncio.gather(
            *(queue.flush_now(timeout_secs=timeout_secs) for queue in self.agents.values())
        )
        return all(results)

    def cancel_translations(self) -> None:
        """Drop pending translation work.

        Deliberately NOT part of ``flush_all``: the agent flush exists to save
        transcript that would otherwise be lost from the notes, whereas a pending
        translation is a live reading aid for a meeting that has just ended.
        Waiting on up to a backlog's worth of model calls would make shutdown slow
        to produce something nobody is looking at.
        """
        if self.translations is not None:
            self.translations.clear()

    def cancel_all(self) -> None:
        for queue in self.agents.values():
            queue.cancel()
        # Every teardown path reaches here (``drain_and_clear`` composes ``clear``,
        # which calls this), so cancelling translations here rather than at each
        # call site is what makes it impossible to leave a worker running against a
        # meeting that is gone.
        self.cancel_translations()

    def resume_all(self) -> list[str]:
        resumed: list[str] = []
        for name, queue in self.agents.items():
            if queue.fail_count > 0:
                queue.resume()
                resumed.append(name)
        return resumed

    async def recover_incomplete_agents(self, timeout_secs: float) -> bool:
        """Retry every incomplete Stop queue and retain protection on failure."""
        queues = [queue for queue in self.agents.values() if queue.drain_incomplete]
        if not queues:
            return True
        results = await asyncio.gather(
            *(queue.recover_incomplete_drain(timeout_secs) for queue in queues)
        )
        return all(results)


# ── lifecycle (metadata side) ───────────────────────────────────────────────


def start_meeting_meta(
    meeting_id: str,
    agents_enabled: list[str] | None = None,
    title: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """Mark a meeting active, refresh its outputs map, seed missing files.

    Self-guarding: holds ``store.meta_transaction()`` across its own
    read-modify-write rather than relying on the caller to. ``_begin_meeting``
    already holds it, which is why the lock is an RLock — see its comment.
    """
    config = store.read_config(root)
    enabled = get_enabled_agents(config, agents_enabled)
    with store.meta_transaction():
        return _start_meeting_meta_locked(meeting_id, agents_enabled, title, enabled, root)


def _start_meeting_meta_locked(
    meeting_id: str,
    agents_enabled: list[str] | None,
    title: str,
    enabled: list[dict[str, Any]],
    root: Path | None,
) -> dict[str, Any]:
    """The read-modify-write itself. Caller holds ``store.meta_transaction()``."""
    meta = store.read_meeting_meta(meeting_id, root) or store.new_meeting_meta(
        meeting_id, title or "Meeting"
    )
    if title:
        meta["title"] = title
    meta["status"] = k.STATUS_ACTIVE
    meta["started_at"] = store.utc_now_iso()
    if agents_enabled is not None:
        meta["agents_enabled"] = agents_enabled

    outputs: dict[str, str] = {}
    for agent_def in enabled:
        try:
            fname = store.agent_output_filename(agent_def)
        except store.MeetingsPathError:
            continue
        if fname:
            outputs[str(agent_def["id"])] = fname
    meta["outputs"] = outputs
    store.write_meeting_meta(meeting_id, meta, root)
    store.ensure_agent_files(meeting_id, enabled, meta.get("title", "Meeting Notes"), root)
    return meta


def end_meeting_meta(meeting_id: str, root: Path | None = None) -> dict[str, Any] | None:
    """Mark a meeting ended. Holds the metadata transaction over its own RMW.

    Stopping a meeting races an in-flight attachment or mute request otherwise: this
    read-modify-write ran unlocked against their locked ones, so whichever wrote
    last silently discarded the other's update.
    """
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None:
            return None
        meta["status"] = k.STATUS_ENDED
        meta["ended_at"] = store.utc_now_iso()
        store.write_meeting_meta(meeting_id, meta, root)
    return meta


# ── agent kickoff prompts ───────────────────────────────────────────────────


def build_meeting_context(meta: dict[str, Any]) -> str:
    """Human-readable meeting context injected into each agent's first message.

    Everything here comes from user/calendar data, so it is redacted before it
    reaches a model prompt that the model may later echo back into chat.
    """
    parts = [f"Meeting: {redact(str(meta.get('title') or 'Meeting'))}"]
    if meta.get("description"):
        parts.append(f"Description: {redact(str(meta['description']))}")
    attendees = meta.get("attendees") or []
    if attendees:
        parts.append("Attendees: " + redact(", ".join(str(a) for a in attendees)))
    attachments = meta.get("attachments") or []
    if attachments:
        parts.append("Attached documents:")
        for att in attachments:
            if not isinstance(att, dict):
                continue
            label = redact(str(att.get("label") or ""))
            kind = att.get("type")
            if kind == "file" and att.get("path"):
                parts.append(f"  - {label}: read the file at {redact(str(att['path']))}")
            elif kind == "url" and att.get("url"):
                parts.append(f"  - {label}: {redact(str(att['url']))}")
    return "\n".join(parts)


def build_init_message(
    agent_def: dict[str, Any],
    meta: dict[str, Any],
    output_path: str,
    cross_ref: str,
) -> str:
    """The first message an agent receives when a meeting starts."""
    prompt = agent_def.get("prompt") or (
        f"You are the {agent_def.get('name') or agent_def.get('id')} agent for this meeting."
    )
    return (
        f"OUTPUT_FILE: {output_path}\n\n"
        f"{prompt}\n\n"
        "Write your output to the exact path in OUTPUT_FILE above. Copy it "
        "character-for-character — do not shorten or modify it.\n"
        "The file already exists — overwrite it directly.\n"
        "IMPORTANT: write the FULL updated file after every transcription batch. "
        "Do not accumulate in memory — write immediately so your output survives "
        "a context limit.\n\n"
        f"Meeting context:\n{build_meeting_context(meta)}\n\n"
        f"{cross_ref}\n\n"
        "Read any attached documents now for context. Do not call a wait, sleep, "
        "polling, or monitoring tool, and do not keep this turn open. Reply with a "
        "brief ready acknowledgment and end this turn. Transcription will arrive in "
        "later messages."
    )


TASK_EXTRACTOR_PROMPT = (
    "You are a meeting task extractor. Listen for action items, assignments, and "
    "follow-ups. Maintain a JSON file with the structure "
    '{"meeting_id": "...", "tasks": [{"id": "t1", "description": "...", '
    '"assignee": "...", "priority": "medium", "status": "open"}], '
    '"updated_at": "..."}. Only extract genuine action items — not discussion '
    "topics or open questions."
)


def build_cross_reference(meeting_dir: str, enabled: list[dict[str, Any]]) -> str:
    """The "here is where the other agents write" block each agent receives."""
    lines: list[str] = []
    for agent_def in enabled:
        try:
            fname = store.agent_output_filename(agent_def)
        except store.MeetingsPathError:
            continue
        if fname:
            name = agent_def.get("name") or agent_def.get("id")
            lines.append(f"  - {name} ({agent_def.get('id')}): {meeting_dir}/{fname}")
    lines.append(f"  - Tasks ({k.TASK_EXTRACTOR_ID}): {meeting_dir}/{k.TASKS_FILE}")
    return "All agent output files (read for cross-reference):\n" + "\n".join(lines)


def _init_agents_plan(
    session: MeetingSession, meta: dict[str, Any], root: Path | None = None
) -> tuple[list[dict[str, Any]], str, str]:
    """Resolve the agent list, meeting dir, and cross-reference block. BLOCKING.

    Runs on a worker thread, never the event loop: ``read_config`` parses
    ``config.json`` and ``meeting_dir`` resolves a path on disk (``resolve()``
    follows symlinks, so it stats every component) before the containment check.

    Grouped into one hop because the whole prologue is derived from one config
    snapshot, and because :func:`init_agents` must then ``await`` a dispatch per
    agent — sequential hops here would add a loop yield before every one of them.
    """
    config = session.config if session.config is not None else store.read_config(root)
    enabled = get_enabled_agents(config, meta.get("agents_enabled"))
    mdir = str(store.meeting_dir(session.meeting_id, root))
    return enabled, mdir, build_cross_reference(mdir, enabled)


async def init_agents(
    session: MeetingSession, meta: dict[str, Any], root: Path | None = None
) -> None:
    """Kick off every enabled agent's session concurrently with its instructions.

    Failures are logged, not raised: one agent that cannot start must not abort
    the meeting for the others. Each agent owns a distinct session slot, so making
    the independent kickoff turns concurrent bounds startup by the slowest agent
    instead of the sum of all agents' turn times.
    """
    enabled, mdir, cross_ref = await asyncio.to_thread(_init_agents_plan, session, meta, root)

    dispatches: list[Awaitable[None]] = []
    for agent_def in enabled:
        try:
            fname = store.agent_output_filename(agent_def)
        except store.MeetingsPathError:
            continue
        if not fname:
            continue
        agent_id = str(agent_def["id"])
        message = build_init_message(agent_def, meta, f"{mdir}/{fname}", cross_ref)
        dispatches.append(_safe_dispatch(session, agent_id, message, agent_def.get("agent") or ""))

    task_message = build_init_message(
        {"id": k.TASK_EXTRACTOR_ID, "name": "Task Extractor", "prompt": TASK_EXTRACTOR_PROMPT},
        meta,
        f"{mdir}/{k.TASKS_FILE}",
        cross_ref,
    )
    dispatches.append(
        _safe_dispatch(session, k.TASK_EXTRACTOR_ID, task_message, k.TASK_EXTRACTOR_AGENT)
    )
    await asyncio.gather(*dispatches)


async def _safe_dispatch(session: MeetingSession, agent_id: str, message: str, agent: str) -> None:
    try:
        await dispatch_to_agent(
            session.sessions,
            slot_key(agent_id, session.meeting_id),
            message,
            agent,
            hooks=session.hooks,
            timeout_secs=_AGENT_INIT_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "meetings: agent %s initialization exceeded %.1fs; continuing",
            agent_id,
            _AGENT_INIT_TIMEOUT_SECS,
        )
        _audit_dispatch(agent_id, outcome="timeout")
        return
    except Exception:
        logger.warning("meetings: could not initialize agent %s", agent_id, exc_info=True)
        _audit_dispatch(agent_id, outcome="error")
        return
    _audit_dispatch(agent_id, outcome="ok")


def _audit_dispatch(agent_id: str, *, outcome: str) -> None:
    try:
        sel().log_tool_invocation(
            session_key="",
            source=f"app:{k.APP_NAME}",
            tool_name="meetings.agent_init",
            tool_kind="agent_dispatch",
            outcome=outcome,
            resources=agent_id,
        )
    except Exception:  # pragma: no cover
        logger.debug("meetings: SEL audit failed for agent init", exc_info=True)


async def broadcast_system(
    session: MeetingSession, message: str, *, timeout_secs: float | None = None
) -> bool:
    """Send a lifecycle notice to every agent, flushing immediately."""
    for queue in session.agents.values():
        queue.enqueue(message)
    return await session.flush_all(timeout_secs=timeout_secs)
