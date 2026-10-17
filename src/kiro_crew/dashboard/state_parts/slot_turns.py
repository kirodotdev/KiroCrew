"""A slot's turn state: the queue and its task, admission, notes, approvals and trust.

The close fence, the live and deferred note buffers, the prompt queue and the
subagent delivery ledger, the running task and its generation, approval futures
and trust scope, question state and the stop machine fields.

Instance fields of ``_ChatSlot`` this module reads and writes:

Writes: ``_acp_client``, ``_active_turn_session_key``, ``_approval_futures``,
``_approval_instances``, ``_approval_stopped``, ``_buffers``, ``_chunk_seq``,
``_closing``, ``_compacting``, ``_coordinator_approvals``, ``_cycle_reached_provider``,
``_dashboard_card_exempt``, ``_dashboard_card_identity``, ``_deferred_notes``,
``_dropped_note_ids``, ``_file_changes``, ``_has_reader_flag``,
``_hook_continuation_depth``, ``_last_enqueue_ts``, ``_last_turn_auth_required``,
``_last_turn_awaiting_permission``, ``_last_turn_children_announced``, ``_lock``,
``_member_approval_seeded``, ``_on_card_event``, ``_on_message``,
``_on_question_retired``, ``_on_row``, ``_pending``, ``_pending_consumers``,
``_pending_context``, ``_pending_release_deferred``, ``_pending_variants``,
``_question_pending``, ``_queue``, ``_queue_persist_inflight``, ``_queue_persist_owed``,
``_queue_persisted_sig``, ``_queue_repository``, ``_responsive``, ``_side``,
``_stop_declined_at``, ``_stop_escalated_card_id``, ``_stop_event_id``,
``_stop_generation``, ``_stop_state_raw``, ``_subagent_delivery_pending``,
``_synthesis_recheck``, ``_task``, ``_trust``, ``_trust_reads``, ``_trust_scope``,
``_trusted_pattern_deadlines``, ``_trusted_patterns``, ``_turn_admission_reserved``,
``_turn_generation``, ``_turn_in_flight_generation``, ``_turn_in_flight_prompt``,
``_turn_reply_mids``, ``_wake_starting_task``, ``credential_evidence``, ``event``,
``linked_session_key``, ``segment_message_id``, ``segment_raw_text``,
``segment_removed``, ``segment_shown_sha``.

A function whose first parameter is ``self`` is a ``_ChatSlot`` method: the
class binds it under the same name (``state_parts/chat_slot.py``), so it reads
and writes the slot's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import traceback
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew.dashboard.slot_buffers import SlotBufferCoordinator
from kiro_crew.dashboard.slot_queue_repository import EMPTY_QUEUE_SIGNATURE, SlotQueueRepository
from kiro_crew.dashboard.state_parts.transcript_rows import row_mid
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.security.credential_sources import CredentialEvidence

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.subagent import SubagentDelivery

logger = logging.getLogger("kiro_crew.dashboard.state")


# Bounds on the persisted-record fallback the subagent panel rebuilds from when
# the in-memory manager does not know a run. Two numbers rather than one
# retention knob, because each defends a different failure:
#
# ``PERSISTED_SUBAGENT_REPLAY_KEEP`` bounds the BURST. Run folders accumulate
# faster than they are reclaimed, so an unbounded rebuild delivers one frame per
# folder the instant a client connects -- the cost
# ``SUBAGENT_REPLAY_BATCH_THRESHOLD`` absorbs downstream, met here at the source.
#
# ``PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS`` bounds RELEVANCE, and is
# deliberately wider than the native terminal TTL above rather than sharing it.
# That TTL bounds cards inside one live session, where an hour is generous. This
# bound has to answer after the gateway process is replaced, and the gap between
# that restart and someone opening the tab is routinely longer than an hour --
# an hour here would leave the panel empty in the exact case the fallback exists
# to serve.
#
# It caps how far back the rebuild REACHES; what is still there to reach is the
# pruner's decision, not this one. ``prune_stale_tombstones`` keeps an abnormal
# ending for its ``max_age_days`` (a week) but reclaims a ``delivered`` folder
# after ``agent.subagent_result_ttl_secs`` (an hour by default). So a day covers
# the interrupted runs a restart leaves behind, which is this fallback's own
# case, while successes delivered longer ago have aged off disk by design and no
# bound here would bring them back.
PERSISTED_SUBAGENT_REPLAY_KEEP = 50


PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS = 86_400.0


# Cap on a slot's queued-completion delivery ledger (see
# ``_ChatSlot.note_pending_subagent_delivery``). Well above any legitimate
# in-flight set — the slot queue itself is capped at 50 rows — so it only ever
# evicts entries left behind by rows that vanished from the queue without being
# consumed, and eviction merely defers those agents' cleanup to the next start.
_MAX_PENDING_SUBAGENT_DELIVERIES = 128


def _delivery_key(content: str) -> str:
    """Identity of a queued completion for delivery bookkeeping.

    A digest of the announce rather than the text itself: a wave digest runs to
    tens of kilobytes, and the ledger only needs to recognise the same announce
    again after a pre-consumption failure re-queues it verbatim under a new
    queue-entry id. Not a security boundary — nothing is authenticated by it.
    """
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:32]


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks.

    Shared by gateway._deliver_result and chat.py queue-drain paths.
    Short-circuits on cancelled tasks (task.exception() would raise CancelledError).
    Exception message is redacted to avoid leaking credentials/URLs to log sinks.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        try:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            redacted_tb, _ = redact_credentials(tb)
            redacted_tb, _ = redact_exfiltration_urls(redacted_tb)
            logger.error("Background task failed:\n%s", redacted_tb)
        except Exception as redaction_err:
            # Include the redaction failure class so bugs in the redactor are visible,
            # without logging the raw traceback (which defeats the redaction contract).
            logger.error(
                "Background task failed (redaction error %s): %s",
                type(redaction_err).__name__,
                type(exc).__name__,
            )


# An arrival over this is refused, not admitted by evicting a seated entry
# the caller already holds a 200 for.
_MAX_PENDING_CONTEXT = 50


def context_entry_expired(entry: dict, now: float) -> bool:
    """True if a pending-context entry's TTL has elapsed.

    Shared by the drain, the per-source cap count, and the deferred-note
    promotion so they cannot disagree about which entries are still live. It
    lives here rather than in chat_runner because ``_ChatSlot`` itself needs it.
    """
    max_age = entry.get("maxAge")
    if max_age is None:
        return False
    return entry.get("injectedAt", 0) + max_age < now


def _note_authorized_elsewhere(stamped: object, live_session: str) -> bool:
    """True when note content records a session other than *live_session*.

    Reads the same ``session`` stamp off a pending-context entry or off a
    transcript row's ``meta``. Absent stamp means not note content that carries
    an authorizing session, so it is never dropped.
    """
    if not isinstance(stamped, dict):
        return False
    authorized = stamped.get("noteSession")
    return authorized is not None and authorized != live_session


def init_turns_fields(self: _ChatSlot) -> None:
    """Set this module's slot fields to their defaults (``_ChatSlot.__init__``)."""
    # Generation of the LOCAL turn ``chat_runner._run_chat`` durably admitted;
    # zero when no local turn is outstanding. Persisted on the metadata line
    # before provider dispatch and omitted after teardown, so a process that
    # dies mid-turn leaves it on disk and every restore path converts it into
    # the interruption row the transcript shape alone cannot prove -- partial
    # assistant text followed by completed tool rows and no error row looks
    # exactly like a finished answer once the process is gone.
    self._turn_in_flight_generation = 0
    # The row that opened the in-flight turn, persisted beside the
    # generation. The row itself rides the periodic flush, so a process
    # death inside that window loses it; the copy here lets the restore
    # put it back before the interruption is judged. None when no turn is
    # in flight.
    self._turn_in_flight_prompt = None
    self._buffers = SlotBufferCoordinator()
    self._queue_repository = SlotQueueRepository(
        id_provider=lambda: uuid.uuid4().hex[:12],
        timestamp_provider=lambda: datetime.now(timezone.utc).isoformat(),
        delivery_key=lambda content: _delivery_key(content),
        max_pending_deliveries=lambda: _MAX_PENDING_SUBAGENT_DELIVERIES,
    )
    # Admission fence while slot deletion spans monitor retirement and history
    # I/O. A DEPTH: two retractions can overlap on one slot, and each must
    # release only its own acquisition (see ``begin_close``).
    self._closing = 0
    # This turn's tool results, reduced to sources and credential
    # fingerprints, so a credential in the reply can name where it came
    # from. Memory only and cleared at every turn start; see
    # ``security.credential_sources``.
    self.credential_evidence = CredentialEvidence()
    # The current segment's text as the model wrote it, before any
    # redaction. The run loop redacts each streamed delta on its own, which
    # can leave pieces of a value a delta boundary cut; the flush saves THIS
    # copy redacted as one text instead (``chat_runner._redact_segment``).
    # Memory only, never persisted, dropped at each flush.
    self.segment_raw_text = ""
    # The hash of the same segment as it streamed (each delta after its own
    # redaction), and each value that redaction replaced, with its tag. The
    # flush saves the raw copy redacted as one text only when the hash
    # proves it is the copy of the streamed text, and withholds it when it
    # still holds one of these values (``chat_runner._redact_segment``). Memory only,
    # dropped with the raw copy.
    self.segment_shown_sha = hashlib.sha256()
    self.segment_removed = {}
    # The backend's id for the model message the current segment's text came
    # from (the last one seen). Written onto the segment's row as
    # ``meta["native_message_id"]`` at the flush, which drops it.
    self.segment_message_id = ""
    self._task = None
    # A send reserved the next turn and is between admission and dispatch.
    self._turn_admission_reserved = False
    # Monotonic publication history for turn ownership. ``task`` returns to
    # None after teardown, so consumers that span awaits cannot distinguish
    # "stayed idle" from "ran and finished" by comparing task references.
    self._turn_generation = 0
    # The turn a structured monitor wake started, until the runner accepts it
    # and the wake's ``nudge`` row is written. Until then the transcript cannot
    # say who opened the running turn, so the projection's ``wake_starting``
    # says it. A refused wake leaves its finished task here, which matches no
    # later ``task``.
    self._wake_starting_task = None
    # Wire seq of the newest chat_chunk this slot has emitted, across turns:
    # the counter never restarts, so a client's replay floor (the seq its
    # transcript already holds) orders every later chunk above it without
    # knowing where one turn ended and the next began.
    self._chunk_seq = 0
    self.event = asyncio.Event()
    self._pending = []
    # Number of readers currently treating ``_pending`` as their delivery
    # queue -- see ``pending_consumer``. Zero means a row left in the queue
    # can never reach a client, which is what makes releasing it safe.
    self._pending_consumers = 0
    # Set when a release was ASKED FOR and refused because a consumer held
    # the queue. Without it the refusal is silent and final: the turn-end
    # purge never runs again for that slot, so the rows it declined to drop
    # outlive every consumer and the leak survives its own fix.
    self._pending_release_deferred = False
    self._queue = []  # [{"id": uuid, "content": str}, ...]
    # Signature of the durable queue value this slot's last committed save
    # wrote (see slot_queue_repository.queue_persist_signature). Drift
    # between it and the live queue is what tells the periodic flush a
    # queued prompt is not on disk yet, so durability does not depend on
    # every queue mutation site remembering to mark the slot dirty. Starts
    # at the EMPTY signature: a slot with nothing queued owes no write, and
    # an unnecessary save would rewrite the transcript and invalidate every
    # cache keyed on its mtime.
    self._queue_persisted_sig = EMPTY_QUEUE_SIGNATURE
    # Single-flight for the immediate queue write (``start_queue_persist``).
    # Loop-affine: set on the event loop, cleared in the future's done
    # callback, which the loop also runs. The executor thread doing the save
    # never reads either one, so they need no lock.
    self._queue_persist_inflight = False
    self._queue_persist_owed = False
    # Newest enqueue instant, read only while ``_queue`` is non-empty — see
    # ``_note_enqueue``.
    self._last_enqueue_ts = ""
    self._approval_futures = {}  # type: ignore[type-arg]
    # Bind the host permission-row identity to the exact future, not the
    # connection-scoped request id that a reconnect can reuse.
    self._approval_instances = {}
    # Approval ids a STOP rejected, rather than a person. A stop resolves the
    # future with an ordinary "rejected", so the runner cannot tell the two
    # apart at the point it records the decision, and its ledger entry would
    # name a person who never answered. The id is added where the stop
    # resolves the future and removed where the runner reads it, so nothing
    # accumulates and a later human rejection on this slot cannot inherit the
    # attribution. Ids rather than a flag, for exactly that reason.
    self._approval_stopped = set()
    self._trust = False  # auto-approve tools for this slot
    # SafetyOverride scope key holding an EXPIRING, SEL-audited auto-approve
    # grant, for an unattended app worker with no human present to click
    # "trust this session". Empty on an ordinary session, and empty is what
    # makes the approval path ignore it entirely. Never a substitute for
    # ``_trust``: this names where the live decision is held, it is not itself
    # the decision — ``safety_override().is_scope_active()`` is.
    self._trust_scope = ""
    self._trust_reads = False  # auto-approve read-only bash commands
    # A crewmate thread took its profile's approval mode (members handler).
    self._member_approval_seeded = False
    self._trusted_patterns = set()  # session-scoped fnmatch globs
    # Timed command-scoped grants, kept apart from ``_trusted_patterns`` so
    # no other writer of that set can end or extend one. Each maps a pattern
    # to its deadline and is pruned at the first decision after it passes.
    # In memory only, so a restart drops timed grants along with every other
    # grant on the slot.
    self._trusted_pattern_deadlines = {}
    # Depth of the current unbroken Stop-hook continuation run: 0 on a normal
    # turn, incremented on each consecutive hook-continuation turn, reset by
    # any turn that is not a hook continuation. Surfaced to Stop hooks as
    # `hook_continuation_count` for diagnostics or stricter hook-owned limits.
    self._hook_continuation_depth = 0
    # Callback for broadcasting messages via global SSE
    self._on_message = None  # Callable[[str, dict], None] | None
    # Every appended row, RECORDED rather than rendered: Callable[[str,
    # dict], None] | None, wired by DashboardState like _on_message. Kept
    # apart from it deliberately — _on_message is the SSE delivery hook and
    # is skipped for a row some other surface already rendered (a user row
    # the composer echoed optimistically) or for a slot with its own HTTP
    # stream reader. Neither says anything about whether the row happened,
    # so a durable record hung off that hook loses exactly the rows a
    # person typed.
    self._on_row = None
    self._on_card_event = None
    self._dashboard_card_identity = uuid.uuid4().hex
    #: Set on a throwaway API slot that no person follows, so automatic
    #: cards never spend the shared budget on it.
    self._dashboard_card_exempt = False
    # Announce stateless question cards this slot retires, so every client
    # drops them: Callable[[str, list[str]], None] | None, wired by
    # DashboardState like _on_message. A retirement that only mutates state
    # is invisible to a second window, and to a /pending response already in
    # flight — either would re-render a card whose answer has been sent.
    self._on_question_retired = None
    # Live ApprovalCoordinator records owned by this slot, wired by
    # DashboardState like _on_message. A sub-agent spawn gate or a tool
    # approval inside a running sub-agent parks its future on the STATE
    # registry, never on _approval_futures, so the projection has to ask
    # the state to learn that this slot is waiting.
    self._coordinator_approvals = None
    self._has_reader_flag = False  # True when HTTP SSE stream is draining
    # True while the session manager runs an automatic compaction on this
    # slot's session. Written by the compacting observer wired in
    # ``wire_session_compact_callback`` and read by the slot projection, so
    # the composer can show the compaction while it runs and the Stop button
    # can warn before a press that would fail it. Not persisted:
    # a compaction never outlives the gateway process.
    self._compacting = False
    # Monotonic time of the last cooperative Stop this slot DECLINED because
    # its session was compacting; 0.0 when none. Kept apart from
    # ``_stop_state`` on purpose: that machine is read by the queue drain as
    # "a stop is in progress" and persists a "Session reset" row on it, and a
    # declined Stop stopped nothing. What the marker buys is the escape
    # hatch: a press that lands within ``STOP_DECLINED_ESCALATION_SECS`` of
    # a decline is the user's second press and escalates to the force stop.
    self._stop_declined_at = 0.0
    self._stop_state_raw = "idle"  # 'idle' | 'soft_pending' | 'killing'
    # Monotonic count of stop INITIATIONS (idle → active edges of
    # _stop_state). Teardown resets _stop_state back to "idle" but never
    # touches this, so long-running decision points (the poisoned-
    # conversation canary probe) can capture it before a wait and detect
    # a Stop that fired AND resolved during the wait — re-reading
    # _stop_state alone would miss it (the exact race documented in
    # chat_handlers._make_stop_resolver).
    self._stop_generation = 0
    self._stop_event_id = None  # transcript message id for in-flight stop
    # Id of the stop card the user escalated to a hard kill, or None. Kept
    # separate from `_stop_state` because turn teardown resets that back to
    # "idle" (see the `_stopping` setter below), which would erase the
    # escalation and let a late cooperative ack relabel the card as a clean
    # stop. Holds an id rather than a bool so the marker cannot leak onto a
    # later card: a boolean left set would make the NEXT card's cooperative
    # ack defer to a hard callback that never fires, stranding it at
    # "stopping". A later press usually mints a fresh uuid, so a stale id
    # stops matching on its own — with ONE exception: a press that finds a
    # same-turn orphan RE-ARMS that card under its existing id
    # (chat_handlers._open_stop_event_card), so that path clears
    # this marker explicitly, and per-attempt identity for the resolver
    # callbacks is carried by `_stop_generation` above, not by the card id.
    self._stop_escalated_card_id = None
    # Set by _run_chat's teardown to that turn's ACP auth-required outcome.
    # The completion-sound gate reads it: a queue held for post-login resume
    # does not count as the session continuing.
    self._last_turn_auth_required = False
    # Whether a turn of the current queue cycle reached a provider (it opened
    # its stream). Set by each turn's tail, read and cleared where the cycle
    # ends (``_finish_queue_cycle``): only such a cycle has a reply to title
    # and summarize, however many local commands or cancelled replays it
    # also ran.
    self._cycle_reached_provider = False
    # Queued sub-agent completions whose delivery tombstone is still owed:
    # queue-id -> the agent ids whose ``result.txt`` that row promises. A
    # completion routed into a BUSY slot is queued, so the parent's context
    # does not contain it until the row drains; the run loop therefore skips
    # its own ``mark_delivered`` and the drain settles these instead, so the
    # retention TTL is measured from consumption rather than from run
    # completion. See ``take_pending_subagent_deliveries``.
    self._subagent_delivery_pending = {}
    self._pending_context = []
    self._deferred_notes = []
    # Note ids dropped at the flush's rebind seam. A dropped
    # note has no delivery obligation left, but its durable entry may only
    # be retired by a save — the ids recorded here are how the next full
    # save knows to retire entries whose rows will never exist.
    self._dropped_note_ids = set()
    # Regenerate feature: variants pending attachment to next finalized assistant message
    self._pending_variants = []
    self._lock = asyncio.Lock()
    self._file_changes = (
        []
    )  # [{path, content, canonical_path, pending_str_replace?}] before-snapshots per turn for chips
    # ``meta.mid`` of every reply row the runner in flight appended this
    # turn (``chat_runner._flush_segment`` / ``_persist_partial_reply``).
    # ``_flush_file_changes`` attaches the turn's chips only to one of these
    # rows: an assistant row another writer injects into the live window
    # mid-turn (a workflow or sub-agent completion) is never this turn's
    # reply, whatever its position. Reset where the turn's start is captured.
    self._turn_reply_mids = []
    self.linked_session_key = ""  # when set, _run_chat uses this as session key
    # Where the turn CURRENTLY in flight actually started, as opposed to
    # where the slot would route a new one. The two diverge whenever the
    # routing above is reassigned on a live slot — a cron injection binds an
    # existing slot to ``cron:<id>`` with no ``running`` gate — and a cancel
    # must address the turn, not the routing. Runtime-only: never persisted,
    # never serialized, empty after a restart, and ``_run_chat`` is its sole
    # lifecycle owner (installed once the turn is committed, cleared after
    # its session is released).
    self._active_turn_session_key = ""
    # Process-local human priority; queue/history metadata cannot restore it.
    self._responsive = None
    self._side = None
    # Live inner AcpClient for the in-flight turn, published by _run_chat at
    # turn start and cleared in its finally. Lets a concurrent request (the
    # dashboard steer handler) reach the running session's client to inject
    # a mid-turn steer. None when idle.
    self._acp_client = None
    # Hang-attribution snapshot stashed by _run_chat's finally just before
    # _acp_client is dropped; read by finish_turn_task when the dashboard
    # ceiling cut the turn (kirocrew.turn.timeout.cause).
    self._last_turn_awaiting_permission = False
    self._last_turn_children_announced = False
    # Agent questions this slot has not answered yet, keyed by the ask's
    # identity: ``{card_id: {"ts": float, "blocking": bool}}``, empty when the
    # agent is not waiting on anything.
    #
    # A question card is a websocket broadcast with no transcript row, so
    # without this record the only surface that knows the agent is waiting is
    # the browser tab that happened to receive the card — a reload, a second
    # window, or the sessions board sees a quiet, finished-looking session.
    #
    # A MAP rather than one slot-wide record because asks overlap: a single
    # field let a second ask overwrite the first, and then whichever resolved
    # first cleared the only record while the other was still parked. Each ask
    # owns its own entry and retires exactly that one. ``blocking``
    # distinguishes an ask_question HTTP round-trip (the turn is parked on the
    # answer) from a stateless card (the turn has ended and the answer arrives
    # as the next message) — the difference between "the agent is stuck" and
    # "the agent is done and asked you something", and which entries a user
    # message may retire.
    self._question_pending = {}


def begin_close(self: _ChatSlot) -> None:
    """Fence new monitor admission before teardown reaches its first await.

    A DEPTH, not a flag, because more than one retraction can be in flight on
    the same slot: a close the person asked for suspends inside its wait for
    guarded history writes, and the bulk stale-slot sweep can reach the same
    slot while it is suspended. With a shared flag, whichever of them finished
    first cleared the fence for both, and the other's remaining awaits then ran
    unfenced -- which is exactly the window the fence exists to close, since
    the dispatch-seam re-reads that are the last line of defence read this
    value.

    Counting instead means each holder releases only its own acquisition, so
    the fence stays up until the last retraction lets go.
    """
    self._closing += 1
    if self._synthesis_recheck is not None:
        self._synthesis_recheck.cancel()
        self._synthesis_recheck = None
    if self._responsive is not None:
        # A responsive steer's grace timer may not outlive its slot.
        self._responsive.cancel_grace()


def cancel_close(self: _ChatSlot) -> None:
    """Release THIS holder's admission fence when teardown leaves the slot live.

    Floors at zero so an unmatched release cannot make the count negative and
    leave a later ``begin_close`` reading as not-closing.
    """
    self._closing = max(0, self._closing - 1)


def push_wire_frame(self: _ChatSlot, cls: str, content: str) -> None:
    """Queue an ephemeral frame for live SSE readers only."""
    self._buffers.push_wire_frame(self, cls, content)


def drain(self: _ChatSlot) -> list[dict[str, str]]:
    """Return and clear pending messages."""
    return self._buffers.drain(self)


def _retry_deferred_release(self: _ChatSlot) -> int:
    """Retry a release refused while a delivery consumer was attached."""
    return self._buffers.retry_deferred_release(self)


@contextlib.contextmanager
def pending_consumer(self: _ChatSlot) -> Iterator[None]:
    """Hold the pending list as an attached delivery queue for the block."""
    with self._buffers.pending_consumer(self):
        yield


def release_pending_chunks(self: _ChatSlot) -> int:
    """Release chunk rows unless a live consumer still owns the queue."""
    return self._buffers.release_pending_chunks(self)


def purge_chunks(self: _ChatSlot) -> int:
    """Drop finalized stream chunks from the transcript and live queue."""
    return self._buffers.purge_chunks(self)


def has_pending_context_seat(self: _ChatSlot) -> bool:
    """True if one more entry fits the queue's seat ceiling.

    Counts live entries plus each held note's context half, because the
    deferred-note flush promotes that half into this same queue -- without the
    reservation a later arrival takes the seat the flush needs and the note's
    context is lost after its 200.
    """
    now = time.time()
    seats = sum(1 for e in self._pending_context if not context_entry_expired(e, now))
    for note in self._deferred_notes:
        ctx = note.get("context")
        if isinstance(ctx, dict) and not context_entry_expired(ctx, now):
            seats += 1
    return seats + 1 <= _MAX_PENDING_CONTEXT


def append_pending_context(self: _ChatSlot, entry: dict[str, Any]) -> bool:
    """Append one built context entry, pruning expired ones and refusing overflow.

    Returns whether the entry was seated. Shared by /context, /note and the
    deferred-note promotion, so the three cannot drift on the ceiling. Expired
    entries are pruned first, which is what frees capacity; an entry that still
    does not fit is refused rather than evicting a live one, and one that arrives
    already expired is dropped rather than seated.

    Every caller must branch on the return: seating is the only thing that makes
    the entry reachable by the drain, so ignoring a ``False`` acknowledges content
    the turn will never see.
    """
    now = time.time()
    # A held note's maxAge can elapse while its turn runs, so an entry can
    # arrive dead; seating it would take a seat from a live one.
    if context_entry_expired(entry, now):
        return False
    self._pending_context[:] = [
        e for e in self._pending_context if not context_entry_expired(e, now)
    ]
    # NO DEDUPLICATION, DELIBERATELY: identical content posted twice on purpose is
    # legitimate, and collapsing it would drop context already answered 200.
    if not self.has_pending_context_seat():
        return False
    self._pending_context.append(entry)
    return True


def drop_foreign_authorized_notes(self: _ChatSlot) -> int:
    """Drop note content whose authorization belongs to another session."""
    return self._buffers.drop_foreign_authorized_notes(
        self,
        authorized_elsewhere=_note_authorized_elsewhere,
        logger=logger,
    )


def deferred_context_count(self: _ChatSlot) -> int:
    """Held notes whose context half has not reached the queue yet."""
    return self._buffers.deferred_context_count(self)


def flush_deferred_notes(self: _ChatSlot) -> int:
    """Flush held notes in order, restoring the unwritten suffix on failure."""
    return self._buffers.flush_deferred_notes(self, logger=logger)


def register_approval(
    self: _ChatSlot, request_id: str, future: asyncio.Future[str], permission_row: dict
) -> None:
    self._approval_futures[request_id] = future
    self._approval_instances.pop(request_id, None)
    mid = row_mid(permission_row)
    if mid:
        self._approval_instances[request_id] = (future, mid)


def approval_instance(self: _ChatSlot, request_id: str, message: dict | None = None) -> str | None:
    instance = self._approval_instances.get(request_id)
    if (
        instance is not None
        and self._approval_futures.get(request_id) is instance[0]
        and not instance[0].done()
        and (message is None or row_mid(message) == instance[1])
    ):
        return instance[1]
    return None


def unregister_approval(self: _ChatSlot, request_id: str, future: asyncio.Future[str]) -> bool:
    owned = self._approval_futures.get(request_id) is future
    if owned:
        self._approval_futures.pop(request_id)
    instance = self._approval_instances.get(request_id)
    if instance is not None and instance[0] is future:
        self._approval_instances.pop(request_id)
    return owned


def mark_permission_resolved(self: _ChatSlot, approval_id: str, decision: str = "approved") -> None:
    """Update the matching stored permission row without marking it dirty."""
    self._buffers.mark_permission_resolved(self, approval_id, decision)


# ── Queue helpers (dict-based queue items) ──


def queue_append(
    self: _ChatSlot,
    content: str,
    kind: str = "",
    meta: dict | None = None,
    *,
    directive_user_origin: bool = False,
    directive_channel_origin: bool = False,
) -> str:
    return self._queue_repository.queue_append(
        self,
        content,
        kind,
        meta,
        directive_user_origin=directive_user_origin,
        directive_channel_origin=directive_channel_origin,
    )


def _note_enqueue(self: _ChatSlot) -> None:
    self._queue_repository.note_enqueue(self)


def queue_insert(
    self: _ChatSlot,
    index: int,
    content: str,
    kind: str = "",
    payload: str = "",
    meta: dict | None = None,
    on_consumed: Callable[[bool], None] | None = None,
    on_irreversibly_consumed: Callable[[], Awaitable[None] | None] | None = None,
    directive_user_origin: bool = False,
    directive_channel_origin: bool = False,
) -> str:
    return self._queue_repository.queue_insert(
        self,
        index,
        content,
        kind,
        payload,
        meta,
        on_consumed,
        on_irreversibly_consumed,
        directive_user_origin,
        directive_channel_origin,
    )


def queue_pop(self: _ChatSlot, index: int = 0) -> dict[str, Any]:
    return self._queue_repository.queue_pop(self, index)


def note_pending_subagent_delivery(
    self: _ChatSlot, content: str, deliveries: list[SubagentDelivery]
) -> None:
    self._queue_repository.note_pending_subagent_delivery(self, content, deliveries)


def owes_subagent_delivery(self: _ChatSlot, contents: list[str]) -> bool:
    return self._queue_repository.owes_subagent_delivery(self, contents)


def take_pending_subagent_deliveries(
    self: _ChatSlot, contents: list[str]
) -> list[SubagentDelivery]:
    return self._queue_repository.take_pending_subagent_deliveries(self, contents)


def queue_remove_by_id(self: _ChatSlot, queue_id: str) -> str | None:
    return self._queue_repository.queue_remove_by_id(self, queue_id)


def queue_edit_by_id(
    self: _ChatSlot,
    queue_id: str,
    content: str,
    *,
    directive_user_origin: bool = False,
    directive_channel_origin: bool = False,
) -> bool:
    return self._queue_repository.queue_edit_by_id(
        self,
        queue_id,
        content,
        directive_user_origin=directive_user_origin,
        directive_channel_origin=directive_channel_origin,
    )


def queue_promote_by_id(self: _ChatSlot, queue_id: str) -> bool:
    return self._queue_repository.queue_promote_by_id(self, queue_id)
