"""Regenerate, variant switch, and edit-resend endpoints."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_delivery import queued_text_for_display
from kiro_crew.dashboard.chat_persistence import _build_message_entry, save_slot_off_loop
from kiro_crew.dashboard.chat_runner import _run_chat, _start_next_queued_turn
from kiro_crew.dashboard.chat_utils import (
    adopt_variant_text,
    effective_session_key,
    redact_display_content,
    reject_if_slot_under_construction,
    slot_history_key,
    variant_from_row,
)
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.remote_relay import remote_bound_refusal
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, row_mid
from kiro_crew.dashboard.system_notices import is_system_notice
from kiro_crew.history import restore_full_rows_off_loop, transcript_stems
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_VARIANTS = 20

# Backoff between retries of the durable recovery write (slot rebound mid-restore
# -> write the removed reply back to its original transcript). A confirmed write
# is the only thing that lets the in-memory recovery be dropped, so a failed
# write is retried rather than settled; the slot stays restore-pending until one
# commits. Small and fixed: the contention this rides out (another writer holding
# the session lock) clears in well under a second.
_RECOVERY_RETRY_DELAY_SECS = 0.25

# How many times the shutdown drain re-attempts a refused recovery write before
# retaining the entry and moving on. Small: the only thing a refusal rides out
# at shutdown is momentary write contention on the session lock, which clears
# well under a second; an entry still unwritten after this is retained (never
# dropped) so persist-before-publish holds.
_RECOVERY_DRAIN_ATTEMPTS = 3

# In-flight regenerate recoveries that still owe a confirmed durable write to the
# ORIGINAL transcript (slot rebound mid-restore). Keyed by a PER-REGENERATE entry
# id, not the transcript — a second regenerate on the SAME transcript (admitted
# once the first's fence releases) must not overwrite the first's still-pending
# entry, which would silently lose the first reply on a stop. Each entry is a
# (conversation_log, transcript_key, rows) record whose transcript_key is where
# the drain writes it; the per-regenerate id only keeps two regenerates' entries
# distinct. A RETRY within one regenerate reuses ITS id (replace, not stack).
# Registered BEFORE a recovery write is awaited (so an in-flight first write is
# visible, not only one that has already failed) and removed once it commits. The
# recovery rows live ONLY here and in the retry closure — never in the rebound
# slot's window — so a periodic flush cannot persist them into the rebound
# conversation. On gateway shutdown, drain_pending_regenerate_recoveries awaits
# an IMMEDIATE write for every remaining entry (no backoff), so a stop landing
# mid-write does not abandon it and leave the original transcript truncated.
_PENDING_RECOVERIES: "dict[str, tuple[Any, str, list[dict], bool, int | None]]" = {}
_PENDING_RECOVERY_SEQ = 0

# SYNCHRONOUS admission reservation. _recovery_admission_refusal reads
# len(_PENDING_RECOVERIES) to confirm registry headroom, but the entry it admits
# is not registered until the pre-turn registration MANY awaits later (the
# truncating save, the slot lock still held but the event loop freed on each
# await). Two concurrent regenerates could therefore both pass admission at the
# 511/512 boundary and both later register, pushing the set to 513 and
# FIFO-evicting an UNPAID older recovery. This counter closes that gap: a
# reservation is taken SYNCHRONOUSLY at admission (no await between the headroom
# check and the increment), admission counts live entries PLUS reservations, and
# the reservation is released either when it becomes a real registry entry (so it
# is not double-counted) or on any aborted-truncation return/exception before
# registration. It never evicts — it only refuses a second admission that would
# over-commit.
_RESERVED_RECOVERIES = 0


def _reserve_recovery() -> None:
    """Take one synchronous admission reservation (no await may intervene)."""
    global _RESERVED_RECOVERIES
    _RESERVED_RECOVERIES += 1


def _release_recovery_reservation() -> None:
    """Release one admission reservation, floored at zero so a double release
    cannot drive the counter negative and loosen the bound."""
    global _RESERVED_RECOVERIES
    _RESERVED_RECOVERIES = _RESERVED_RECOVERIES - 1 if _RESERVED_RECOVERIES > 0 else 0


# A per-entry SETTLE GATE, keyed by the same entry id as _PENDING_RECOVERIES. A
# fallback (pre-turn) entry's owning regenerate finishes by running a done-
# callback that creates a confirm-or-restore task, and THAT task is what persists
# the reply and then clears the entry. The shutdown drain writes the entry's rows
# straight to the original transcript; if it runs while that attach/confirm task
# is still mid-flight it would write the reply the task is ALSO about to persist,
# duplicating it. So the gate holds the awaitables the drain must settle first —
# the turn task and (once created) its attach/confirm/restore task — and the
# drain awaits the gate, then RE-READS _PENDING_RECOVERIES: if the settled task
# already committed and dropped the entry, the drain writes nothing. An entry
# with no gate (an in-flight _recover_to_original_transcript write) has nothing to
# settle — it IS the write — and the drain carries it directly.
_PENDING_RECOVERY_GATES: "dict[str, _RecoverySettleGate]" = {}


class _RecoverySettleGate:
    """The awaitables the shutdown drain must settle before writing one entry.

    ``turn`` is the regenerate turn task; ``settle`` is the attach/confirm/restore
    task its done-callback creates (filled in later, so a mutable holder), which is
    the task that persists the reply and drops the entry on success. The drain
    quiesces ``turn`` first — a turn still in flight at shutdown is cancelled and
    awaited so its done-callback runs and populates ``settle`` (or drops the
    entry), rather than letting the drain write the row the turn's own
    replacement save would also persist — then awaits ``settle``, and re-checks
    the registry afterwards so a settled task that already persisted-and-dropped
    the entry is not written a second time.
    """

    __slots__ = ("turn", "settle")

    def __init__(self, turn: "asyncio.Task | None") -> None:
        self.turn = turn
        self.settle: "asyncio.Task | None" = None


async def _settle_gate(gate: "_RecoverySettleGate | None") -> None:
    """Quiesce the owning turn and settle its attach/confirm/restore task before
    the drain writes one entry, so the drain and the live turn never both write
    the row.

    A shutdown can land mid-regenerate BEFORE the turn emits its first segment.
    The turn is then still running, but it still owns a terminal path — its
    done-callback will create the attach/confirm/restore task that persists the
    reply. If the drain wrote the fallback entry now and then the turn's own
    replacement save ran, the old reply would be persisted TWICE: standalone by
    the drain AND retained as a variant by the replacement. So an in-flight turn
    is not a reason to skip settling — it is a reason to QUIESCE it: cancel the
    turn, await its completion (its done-callback fires during that completion,
    before this coroutine resumes, populating ``settle`` or dropping the entry),
    then await ``settle``. All awaits are best-effort and shielded — the drain
    must proceed whatever the tasks' outcome — but it proceeds only AFTER the
    live turn's own write path has settled, so exactly one writer touches the row.
    """
    if gate is None:
        return
    turn = gate.turn
    if turn is not None and not turn.done():
        # Shutdown mid-turn. Cancel the turn and await it so its done-callback
        # runs and either persists the reply (dropping the entry) or fills in the
        # settle task below. Without this quiesce the drain would write the row
        # standalone while the turn's own replacement save also persists it.
        turn.cancel()
        try:
            await asyncio.shield(turn)
        except Exception:  # noqa: BLE001 - best-effort quiesce before drain
            pass
        except asyncio.CancelledError:
            pass
    settle = gate.settle
    if settle is None or settle.done():
        return
    try:
        await asyncio.shield(settle)
    except Exception:  # noqa: BLE001 - best-effort settle before drain
        pass
    except asyncio.CancelledError:
        pass


#: Retention bound on _PENDING_RECOVERIES — a plain ENTRY-COUNT cap. Entries are
#: popped on a confirmed write, so the live set is normally tiny (the in-flight
#: recoveries). This cap is a backstop so a pathological run — many regenerates
#: whose durable write never commits (a full / read-only data home), each
#: leaving a bounded-retry entry for the drain — cannot grow the registry
#: without limit. A COUNT cap bounds the registry deterministically with no
#: dependency on the durable write: on overflow the OLDEST entry is dropped
#: (FIFO, insertion order), bounding the registry even when every write is
#: failing. (A byte/payload cap enforced by flushing the evicted entry would be
#: circular — the only thing that fills the registry is write failure, so a
#: flush-on-eviction would also fail and bound nothing — so the bound is a plain
#: count, not a byte budget.)
_PENDING_RECOVERIES_MAX = 512

#: Named upper bound, in bytes, on the SERIALIZED recovery payload one
#: regenerate may hold pending (the rows the drain would write back). The count
#: cap above bounds how MANY entries the registry keeps; this bounds how LARGE a
#: single entry's owed payload can be. Enforced at ADMISSION — before the
#: destructive truncation — not by evicting an already-admitted entry, so it is
#: not circular: a payload over this bound (or a registry already at its count
#: cap) refuses the regenerate with the previous reply STILL INTACT on the
#: window and transcript, rather than truncating first and then discovering the
#: recovery cannot be retained. 8 MiB is far above any real reply yet caps a
#: pathological transcript row from pinning unbounded bytes for the drain.
_MAX_RECOVERY_PAYLOAD_BYTES = 8 * 1024 * 1024


def _recovery_payload_bytes(rows: "list[dict]") -> int:
    """Serialized byte size of the rows a recovery entry would hold.

    Mirrors how the durable write serializes rows (JSON), so the admission bound
    reflects what the registry actually retains. Best-effort: a row that cannot
    be JSON-encoded falls back to ``repr`` sizing so an un-encodable payload is
    bounded rather than silently treated as weightless.
    """
    try:
        import json

        return len(json.dumps(rows, default=repr).encode("utf-8"))
    except Exception:  # noqa: BLE001 - sizing must never raise into admission
        return len(repr(rows).encode("utf-8"))


def _recovery_admission_refusal(rows: "list[dict]") -> "web.Response | None":
    """Refuse admission (return a 409) when a recovery for *rows* cannot be
    reserved, else None.

    Called BEFORE the regenerate truncates history, so a refusal leaves the
    previous reply intact. Two reservations, both named:
      * serialized-payload bound — the rows the drain would owe must fit
        _MAX_RECOVERY_PAYLOAD_BYTES;
      * registry capacity — admitting this entry must not push the live set past
        _PENDING_RECOVERIES_MAX (reserve-before-truncate, so a full registry
        refuses rather than FIFO-dropping an older owed reply to make room).
    Fail-closed: if either reservation cannot be met the regenerate is refused
    and nothing is truncated.
    """
    payload = _recovery_payload_bytes(rows)
    if payload > _MAX_RECOVERY_PAYLOAD_BYTES:
        logger.warning(
            "Regenerate: refusing — recovery payload %d bytes exceeds the %d-byte bound; "
            "leaving the previous reply intact rather than truncating a reply that cannot be "
            "retained for recovery",
            payload,
            _MAX_RECOVERY_PAYLOAD_BYTES,
        )
        return web.json_response(
            {"error": "reply too large to regenerate safely", "code": "recovery_payload_too_large"},
            status=409,
        )
    if len(_PENDING_RECOVERIES) + _RESERVED_RECOVERIES >= _PENDING_RECOVERIES_MAX:
        logger.warning(
            "Regenerate: refusing — no recovery capacity (registry at %d live + %d reserved of "
            "%d); leaving the previous reply intact rather than truncating with no room to retain "
            "its recovery",
            len(_PENDING_RECOVERIES),
            _RESERVED_RECOVERIES,
            _PENDING_RECOVERIES_MAX,
        )
        return web.json_response(
            {"error": "too many pending recoveries", "code": "recovery_capacity_exhausted"},
            status=409,
        )
    return None


def _forget_pending_recovery(entry_key: str) -> None:
    """Remove an entry from the registry. The single pop path."""
    _PENDING_RECOVERIES.pop(entry_key, None)
    _PENDING_RECOVERY_GATES.pop(entry_key, None)


def _register_pending_recovery(
    entry_key: str, value: "tuple[Any, str, list[dict], bool, int | None]"
) -> None:
    """Insert a pending-recovery entry under *entry_key*, bounded by a plain
    entry-count cap.

    Re-inserting an existing key refreshes it in place (it keeps its position
    for a retry within one regenerate — the common replace). A brand-new key
    that pushes the registry past the cap drops the OLDEST entry (FIFO), so the
    set never grows without limit — deterministically, with no dependency on the
    durable write that the eviction would otherwise circularly re-attempt.
    """
    existed = entry_key in _PENDING_RECOVERIES
    _PENDING_RECOVERIES[entry_key] = value
    if not existed:
        while len(_PENDING_RECOVERIES) > _PENDING_RECOVERIES_MAX:
            oldest = next(iter(_PENDING_RECOVERIES))
            if oldest == entry_key:
                break
            _PENDING_RECOVERIES.pop(oldest, None)
            _PENDING_RECOVERY_GATES.pop(oldest, None)
            logger.warning(
                "Regenerate: _PENDING_RECOVERIES exceeded %d entries; dropping the oldest pending "
                "recovery (key=%s) to bound the registry — its reply was still owed a durable "
                "write and the shutdown drain will no longer carry it",
                _PENDING_RECOVERIES_MAX,
                value[1],
            )


async def drain_pending_regenerate_recoveries(_app: "Any" = None) -> None:
    """Flush any in-flight regenerate recovery writes before the loop stops.

    Registered as an ``on_shutdown`` handler. A recovery whose first write
    failed retries on a background task; without this drain a gateway stop
    landing between the failure and the next retry would abandon the task and
    leave the reply lost (the original transcript stays truncated). This awaits
    an IMMEDIATE confirmed write for every pending entry — no backoff between
    entries — and removes an entry ONLY once its write returns True. A refused
    write (write contention) is retried a bounded number of times within the
    drain; an entry that still cannot be written is RETAINED in the registry
    rather than dropped, so persist-before-publish holds — the reply is never
    treated as recovered until the durable write actually commits. A deleted
    transcript returns True (honored by ``restore_full_rows_off_loop``) and is
    correctly removed; only a genuinely unwritable disk leaves an entry behind,
    which is a loss nothing in-process can prevent.
    """
    pending = list(_PENDING_RECOVERIES.items())
    for entry_id, _snapshot in pending:
        # Settle this regenerate's turn and its attach/confirm/restore task
        # BEFORE writing. That task is what persists the reply and then drops the
        # entry; draining ahead of it would write the same rows the task is about
        # to persist, duplicating the reply on the transcript. After the gate
        # settles, RE-READ the entry — a task that committed will have dropped it,
        # and there is then nothing (and must be nothing) to write.
        await _settle_gate(_PENDING_RECOVERY_GATES.get(entry_id))
        live = _PENDING_RECOVERIES.get(entry_id)
        if live is None:
            # The settled attach/confirm/restore task already persisted the reply
            # and dropped the entry — writing now would duplicate it.
            continue
        conversation_log, key, rows, existed_at_truncation, deletion_generation = live
        committed = False
        for _attempt in range(_RECOVERY_DRAIN_ATTEMPTS):
            try:
                committed = await restore_full_rows_off_loop(
                    conversation_log,
                    key,
                    rows,
                    existed_at_truncation=existed_at_truncation,
                    deletion_generation_at_truncation=deletion_generation,
                )
            except Exception:  # noqa: BLE001 - best-effort shutdown flush
                logger.warning(
                    "Regenerate: shutdown drain of a pending recovery to %s raised",
                    key,
                    exc_info=True,
                )
                committed = False
            if committed:
                break
        if committed:
            # Confirmed on disk — safe to drop the entry.
            _forget_pending_recovery(entry_id)
        else:
            # The write could not commit; RETAIN the entry so the reply is not
            # treated as recovered when it is still deleted on disk.
            logger.warning(
                "Regenerate: shutdown drain could not commit the pending recovery to %s after "
                "%d attempts; retaining it rather than dropping the only recoverable copy",
                key,
                _RECOVERY_DRAIN_ATTEMPTS,
            )


# Longest replacement prompt edit-resend accepts, in characters. Named here
# rather than inlined so the endpoint's cap is greppable; the value matches the
# sibling boundaries (``chat_rewind``'s ``content`` and ``chat_fork``'s
# ``prompt``), which is the point -- one edit of the same message must not be
# accepted by one endpoint and refused by another.
_MAX_EDIT_CONTENT_CHARS = 32_768

# How many times the edit-resend cancellation path re-shields the in-flight
# history rewrite before giving up on learning its outcome. Each retry absorbs
# ONE further cancellation (a gateway shutdown landing on a handler already
# unwinding from a client disconnect), so this bounds a cancel storm rather than
# a duration -- the worker thread itself cannot be interrupted and always
# finishes. Giving up leaves the live slot untouched, which is the safe half of
# the desync: a stale-but-complete window rather than a committed edit nothing
# persisted.
_SAVE_DRAIN_ATTEMPTS = 8


def _same_transcript(key_a: str, key_b: str) -> bool:
    """Whether two history keys address the same on-disk transcript file.

    A channel-born slot's key has more than one legitimate spelling for the same
    session -- the bare ``<ts>`` / ``slack:<ts>`` / ``dashboard:slack_<ts>``
    forms all resolve to the same file -- and a reconciler can rebind an unbound
    channel slot mid-turn, changing which spelling ``slot_history_key`` returns
    without changing the underlying transcript. A bare string compare would then
    read the rebind as a different conversation. Comparing the filename STEMS the
    keys could occupy (``transcript_stems``, canonical + legacy) answers the real
    question -- same file -- so the restore is not abandoned over a respelling.
    """
    if key_a == key_b:
        return True
    return bool(set(transcript_stems(key_a)) & set(transcript_stems(key_b)))


def _destructive_history_busy(slot: "_ChatSlot") -> web.Response | None:
    """Refuse history mutation while a turn, admission reservation, teardown, or
    an in-flight reply-restore owns the slot.

    The teardown arm is what keeps a truncating save from being ADMITTED into a
    close that is already running. A close fences the slot synchronously before
    its first await and then waits for a guarded history write to leave its commit
    window; without this arm a regenerate arriving during that wait would dispatch
    a fresh truncating write which the close has already stopped waiting for, and
    it could commit onto the transcript after the replacement has adopted the key.
    ``cancel_close`` releases the fence on every path that leaves the slot live, so
    an aborted close re-admits the mutation instead of wedging the tab.

    The restore arm covers the window a regenerate opens after its turn ends
    empty. The done-callback schedules ``_restore_previous_reply``, which splices
    the removed rows back onto the live window and then AWAITS a guarded disk
    save. The turn task is already finished by then, so ``turn_running`` reads
    False and the slot looks idle; a second regenerate/edit-resend/switch-variant
    landing in that await would mutate (or re-truncate) the half-restored window,
    and the restore's own merge save would re-broadcast rows that the second
    mutation has since removed. Treating a pending restore task as busy fences
    those mutations behind the restore's commit. The task clears itself to None in
    a done-callback, so a completed restore stops reading as busy.
    """
    if slot.turn_running:
        return web.json_response({"error": "slot is running", "code": "slot_running"}, status=409)
    if slot.running:
        return web.json_response({"error": "slot is busy", "code": "slot_busy"}, status=409)
    if slot.is_closing:
        return web.json_response({"error": "slot is closing", "code": "slot_closing"}, status=409)
    # The restore-pending marker spans the WHOLE restore-in-flight window —
    # including the instant between the empty turn finishing and its done-callback
    # creating the restore task, where the task handle is still None. Checking the
    # marker (not just the task) is what closes that pre-task gap. The task check
    # stays as a belt-and-braces read for any path that sets the task without the
    # marker.
    if getattr(slot, "_regenerate_restore_pending", False):
        return web.json_response(
            {"error": "slot is restoring a reply", "code": "slot_restoring"}, status=409
        )
    restore_task = getattr(slot, "_regenerate_restore_task", None)
    if restore_task is not None and not restore_task.done():
        return web.json_response(
            {"error": "slot is restoring a reply", "code": "slot_restoring"}, status=409
        )
    return None


async def api_chat_slot_regenerate(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/regenerate — regenerate the last assistant reply."""
    # Destructive: this truncates and PERSISTS history before the background
    # turn runs, so a failed turn cannot undo it. Unlike an ordinary send, the
    # readiness latch must be honored BEFORE the mutation.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    under_construction = reject_if_slot_under_construction(state, slot)
    if under_construction is not None:
        return under_construction

    # A crew-bound slot has no local regenerate: it would truncate LOCAL history
    # and re-run the turn on this machine, diverging from the peer.
    refusal = remote_bound_refusal(slot)
    if refusal is not None:
        return refusal

    async with slot._lock:
        busy = _destructive_history_busy(slot)
        if busy is not None:
            return busy

        msgs = slot.messages
        ai_idx = -1
        for i in range(len(msgs) - 1, -1, -1):
            role = msgs[i].get("role")
            # Never cross a real user turn: the truncation below deletes
            # everything after the target reply's user row, so a reply found
            # PAST a newer user row (e.g. a /compact row awaiting only its
            # notice) would take that newer turn with it, irreversibly.
            if role == "user":
                break
            if role != "assistant":
                continue
            # A system notice (compaction / session reload) is a status row,
            # not the reply being regenerated: capturing it as the variant
            # would silently drop the real reply from variant history. The
            # frontend's optimistic truncation runs the same skip.
            if is_system_notice("assistant", msgs[i].get("meta")):
                continue
            ai_idx = i
            break
        if ai_idx < 0:
            return web.json_response(
                {"error": "no assistant message to regenerate", "code": "no_assistant_message"},
                status=400,
            )
        u_idx = -1
        for i in range(ai_idx - 1, -1, -1):
            if msgs[i].get("role") == "user":
                u_idx = i
                break
        if u_idx < 0:
            return web.json_response(
                {"error": "no preceding user message", "code": "no_user_message"}, status=400
            )

        user_msg = msgs[u_idx].get("content", "")
        if not user_msg:
            return web.json_response(
                {"error": "empty user message", "code": "empty_user_message"}, status=400
            )

        ai_msg = msgs[ai_idx]
        _rv = ai_msg.get("variants")
        variants: list[dict] = list(_rv) if isinstance(_rv, list) else []  # type: ignore[arg-type]
        current_entry = variant_from_row(ai_msg)
        if not any(v.get("content") == current_entry["content"] for v in variants):
            variants.append(current_entry)
        if len(variants) > _MAX_VARIANTS:
            variants = variants[-_MAX_VARIANTS:]
        # The previous reply's full variant chain, kept for the restore closure.
        # If the turn ends with a PARTIAL reply (not an empty turn), the restore
        # attaches these to that partial reply as variant history rather than
        # losing them — mirroring _flush_segment, which adopts _pending_variants
        # onto a normally-flushed reply. A deep copy because the live rows are
        # mutated below and by the turn.
        removed_variants = copy.deepcopy(variants)

        # The rows this truncation removes, kept whole so the previous reply can
        # be put back if the regenerated turn ends without ever producing one.
        # A deep copy because the live list is mutated below and again by the
        # turn; the restore must reinstate the reply as it stood, not a later
        # aliased shape. u_idx + 1 is the first removed row, so index 0 of this
        # list is the reply being regenerated.
        removed_rows = copy.deepcopy(slot.messages[u_idx + 1 :])
        # Reserve recovery capacity BEFORE truncating: bound the serialized
        # payload the recovery would owe and confirm the registry has room. A
        # refusal here returns with the previous reply STILL on the window and
        # transcript — nothing has been truncated yet — which is why this is not
        # the circular eviction-flush bound: admission is checked before the
        # destructive mutation, not by dropping an entry already admitted.
        _admission = _recovery_admission_refusal(removed_rows)
        if _admission is not None:
            return _admission
        # Take the reservation SYNCHRONOUSLY — no await between the headroom
        # check inside _recovery_admission_refusal and this increment — so a
        # second concurrent regenerate at the boundary sees this reservation and
        # is refused rather than both passing and later evicting an unpaid entry.
        # The reservation is released exactly once: when the pre-turn
        # registration converts it into a real registry entry (so it is not
        # double-counted), or on each aborted-truncation return path before that
        # registration (the two save-refused branches below). The span between
        # admission and registration contains no await that can raise unhandled
        # (the truncating save and its retry are each wrapped so a failure
        # becomes a handled return, not a propagating exception), so these
        # explicit releases cover every path the reservation must not outlive.
        _reserve_recovery()
        # Whether the ORIGINAL transcript file existed BEFORE this truncation.
        # Threaded into the recovery so a later absence can be told apart: a
        # transcript that existed here and is gone at recovery time was DELETED
        # (honor it, settle); one that never existed was NEVER PERSISTED (its
        # truncation never committed) and its absence is a real loss to recover,
        # not a delete to honor. Captured now, on the event loop, before any
        # await moves the slot's routing.
        transcript_existed_at_truncation = (
            state.conversation_log is not None
            and state.conversation_log._path(slot_history_key(slot)).exists()
        )
        # The transcript's DELETION GENERATION at truncation. Threaded into the
        # recovery and rechecked under the write lock so a delete that lands
        # during the regenerate — even one followed by a same-name recreate (the
        # user clears the thread then posts to it again) — is detected as a
        # generation bump and the stale reply is NOT written into whatever now
        # holds the name. The existence bit above cannot catch the recreate case
        # (the file is present again); the generation can. Captured now, on the
        # event loop, before any await moves the slot's routing.
        transcript_deletion_generation = (
            state.conversation_log.deletion_generation(slot_history_key(slot))
            if state.conversation_log is not None
            else None
        )
        # The user row's STABLE id. u_idx is a numeric position captured now, but
        # the turn appends rows (chunk rows count toward _MAX_SLOT_MESSAGES until
        # the turn-end purge), so a front-trim during the turn can shift every
        # position down and leave u_idx pointing past the user row. The restore
        # relocates the user row by this id and splices after it, rather than
        # trusting the stale index.
        restore_anchor_mid = row_mid(slot.messages[u_idx])
        del slot.messages[u_idx + 1 :]
        slot.invalidate_source_links()
        slot._dirty = True
        slot._resumed_count = 0
        # Window was truncated → next save MUST be the archive-safe rewrite path.
        # If the inline save below fails, the flag keeps the flush loop on the
        # rewrite path so the dropped tail is still archived.
        slot._pending_rewrite = True
        slot._pending_variants = variants
        # Commit to the restore path NOW, synchronously with the truncation that
        # removed the reply — before the turn is even dispatched. This marks the
        # slot restore-pending for the WHOLE window a teardown must not archive
        # across, with no gap: slot.running and _regenerate_restore_task each
        # cover only PART of it (running drops when the empty turn ends, the task
        # does not exist until the done-callback runs a tick later), so a fence
        # keyed on either alone has an instant where it reads idle. This flag is
        # cleared on exactly the two terminal paths — _flush_segment consuming
        # the variants (a reply landed, no restore needed) or the restore task
        # settling — so it is true across the entire interval and false outside
        # it.
        slot._regenerate_restore_pending = True

        # Pin the transcript and the slot object this truncation was authorized
        # against. Both are read BEFORE the write's await: that await frees the
        # event loop while the worker thread runs, and a same-name
        # close-and-recreate is NOT serialized against this slot._lock (the
        # cleanup pops state._slots[name] and get_or_create_slot re-inserts,
        # neither taking the original lock).
        #
        # Two axes can move, and they need two checks, matching the pair
        # edit-resend below carries:
        #   * routing -- save_slot_off_loop refuses the write (returns False,
        #     nothing written) when the slot's routing resolves to a different
        #     key at write time. This catches a RENAMED replacement.
        #   * object identity -- a same-name recreate that resumes the same
        #     transcript keeps the history key identical, so the routing check
        #     passes and the stale rewrite would land on the replacement anyway.
        #     expected_slot_name carries this slot's map key into the save, where
        #     state._slots[name] is re-read at the locked commit boundary with no
        #     await before the write: if the map holds a different slot the
        #     save refuses (returns False). A pre-dispatch check cannot cover it
        #     because the recreate can land inside the executor wait, after the
        #     check and before the write.
        #
        # On either refusal the original slot is being torn down and its
        # regeneration has no future, so nothing that would otherwise persist is
        # lost; both refusals are recorded in the save's own log lines.
        # best_effort keeps the site fire-and-forget: a genuine transient
        # failure re-arms _dirty (and _pending_rewrite is already set) so the
        # periodic flush retries.
        expected_history_key = slot_history_key(slot)
        try:
            msgs_snapshot = list(slot.messages)
            committed = await save_slot_off_loop(
                state,
                slot,
                msgs_snapshot,
                expected_history_key=expected_history_key,
                expected_slot_name=name,
            )
        except Exception:
            logger.warning("Regenerate: failed to rewrite session history", exc_info=True)
            committed = True
        if not committed:
            # The inline truncating save was refused. Two very different causes,
            # told apart by whether the slot still resolves to the transcript the
            # truncation was authorized against:
            #
            #   * A GENUINELY DIFFERENT transcript, or a same-name replacement
            #     (state._slots[name] holds a different object): the original slot
            #     is being torn down and its truncation exists only in this
            #     popped slot's memory — nothing persisted, nothing to recover.
            #     Abort without dispatching; the replacement owns the transcript.
            #
            #   * The SAME transcript, merely RESPELLED (an unbound channel slot
            #     rebinding slack_<ts> -> slack:<ts> during the write): the
            #     conversation is unchanged and still live, but the save guard
            #     compares the raw key string and refused on the spelling. The
            #     truncation already removed the old reply from the window and
            #     armed _pending_rewrite, so simply returning 409 here would clear
            #     the restore marker and leave the periodic rewrite to DURABLY
            #     truncate the old reply. Instead, retry the save ONCE with the
            #     slot's current key spelling; if it commits, the regenerate
            #     proceeds normally. If it still will not commit, UNDO the
            #     truncation — re-insert the removed rows, drop the stashed
            #     variants, and clear _pending_rewrite — so the old reply survives
            #     on the (same) transcript rather than being rewritten away.
            if state._slots.get(name) is slot and _same_transcript(
                slot_history_key(slot), expected_history_key
            ):
                retry_committed = False
                try:
                    retry_committed = await save_slot_off_loop(
                        state,
                        slot,
                        list(slot.messages),
                        expected_history_key=slot_history_key(slot),
                        expected_slot_name=name,
                    )
                except Exception:
                    logger.warning(
                        "Regenerate: retry rewrite after a key respelling failed for %s",
                        slot.key,
                        exc_info=True,
                    )
                if retry_committed:
                    committed = True
                else:
                    # Could not persist the truncation even on the current key:
                    # undo it so the old reply is not lost to the periodic
                    # rewrite. The slot is left as if the regenerate never
                    # truncated — same transcript, reply intact.
                    restored_tail = copy.deepcopy(removed_rows)
                    # Relocate the user row by its stable mid and splice the
                    # removed tail immediately AFTER it, rather than appending at
                    # end-of-list. During the retry save-await above another
                    # writer (a workflow/cron injection, a queued follow-up's
                    # user row) can append rows onto the live window; appending
                    # the old reply at the end would then order it AFTER those
                    # newer rows, and the periodic flush would persist the
                    # transcript as user -> newer rows -> OLD reply (wrong
                    # order). Splicing at the anchor restores user -> reply ->
                    # whatever followed. If the anchor row is gone (front-trimmed
                    # off the window, or it carried no mid), fall back to the
                    # captured index clamped into range.
                    _undo_anchor_pos = (
                        next(
                            (
                                i
                                for i, r in enumerate(slot.messages)
                                if row_mid(r) == restore_anchor_mid
                            ),
                            None,
                        )
                        if restore_anchor_mid
                        else None
                    )
                    _undo_at = (
                        (_undo_anchor_pos + 1)
                        if _undo_anchor_pos is not None
                        else min(u_idx + 1, len(slot.messages))
                    )
                    slot.messages[_undo_at:_undo_at] = restored_tail
                    slot._enforce_message_bound()
                    slot._pending_variants = []
                    slot._pending_rewrite = False
                    slot._regenerate_restore_pending = False
                    slot.invalidate_source_links()
                    slot._dirty = True
                    # The truncation was undone and no recovery will be
                    # registered; release the admission reservation so it does
                    # not permanently narrow the registry's capacity.
                    _release_recovery_reservation()
                    logger.warning(
                        "Regenerate: inline rewrite could not commit on the respelled key for "
                        "%s; undid the truncation so the previous reply survives on the same "
                        "transcript",
                        slot.key,
                    )
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "could not save; the previous reply was kept, retry",
                            "code": "regenerate_save_refused",
                        },
                        status=409,
                    )
        if not committed:
            # A genuinely different transcript or a same-name replacement: the
            # truncation exists only in this popped slot's in-memory window.
            # Dispatching _run_chat now would run a turn on a removed slot and
            # persist its truncated branch over the replacement's transcript, so
            # abort without dispatching.
            logger.warning(
                "Regenerate: history save refused for %s (concurrent delete or recreate); "
                "not dispatching the turn",
                slot.key,
            )
            # No turn will run, so no restore will ever be scheduled: release the
            # restore-pending marker set at truncation so the slot does not read
            # busy forever.
            slot._regenerate_restore_pending = False
            # No recovery will be registered on this aborted path; release the
            # admission reservation.
            _release_recovery_reservation()
            state.push_slots_update()
            return web.json_response(
                {
                    "error": "the conversation changed while saving; retry",
                    "code": "regenerate_save_refused",
                },
                status=409,
            )

        sel().log_api_access(
            caller="dashboard",
            operation="chat.regenerate",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )

        hint = (
            "The user regenerated the previous response. Produce a fresh answer — "
            "vary phrasing, structure, or angle. Do not say you already answered or "
            "reference the prior reply."
        )
        # Holds THIS turn's reply ids, captured the instant _run_chat returns
        # (inside the turn's own frame, below), before control reaches any
        # dispatcher that could start a queued follow-up. _run_chat clears
        # slot._turn_reply_mids at the top of every turn, and a successor turn
        # can run that clear before this turn's done-callback does, so the
        # callback must read ONLY this captured value and never the live list.
        captured_reply_mids: dict[str, set[str]] = {}

        async def _run_regenerate_turn() -> None:
            try:
                await _run_chat(
                    state,
                    slot,
                    user_msg,
                    regenerate_hint=hint,
                    _directive_user_origin=not bool(request.get("app", "")),
                    # See ``api_chat``: an observed app must be NAMED, because
                    # the actor resolver's fallback is ``user``. ``""`` is the
                    # parameter's own default and reads as "not named".
                    _turn_actor="app" if request.get("app", "") else "",
                )
            finally:
                # Synchronous on return, before this coroutine yields again: a
                # queued successor dispatched by a caller up the stack has not
                # run yet. Read the consume-safe accumulator, not
                # slot._turn_reply_mids — the latter is cleared by _flush_segment
                # and by _flush_file_changes (which runs inside _run_chat's own
                # finally, before this one), so a file-writing partial reply
                # would leave it empty. _turn_reply_mids_all keeps every reply id
                # this turn produced until the next turn starts.
                captured_reply_mids["mids"] = set(getattr(slot, "_turn_reply_mids_all", None) or [])

        task = asyncio.create_task(_run_regenerate_turn())
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        # The transcript this restore is authorized against, captured on the
        # event loop BEFORE any await. Resolving it inside the async restore
        # below would read whatever a mid-turn cron/workflow rebind had moved
        # the slot to, defeating the guard -- the same hazard the edit-resend
        # path documents for its own commit boundary.
        restore_expected_key = expected_history_key

        # A per-regenerate registry key so a SECOND regenerate on this same
        # transcript (admitted once this one's fence releases) registers under a
        # DISTINCT key and cannot overwrite this regenerate's still-pending
        # entry. A retry WITHIN this regenerate reuses this same id (replace, not
        # stack). The entry's stored transcript_key stays restore_expected_key,
        # so the drain still writes to the right transcript regardless of this
        # id.
        global _PENDING_RECOVERY_SEQ
        _PENDING_RECOVERY_SEQ += 1
        recovery_entry_key = f"{restore_expected_key}#regen{_PENDING_RECOVERY_SEQ}"

        def _canonical_recovery_rows(rows: "list[dict]") -> "list[dict]":
            # Build the CANONICAL persisted representation of each row (the same
            # shape the slot save writes) so a recovered reply lands
            # byte-equivalent — original ts, provenance, full meta, tools and
            # variants preserved, redaction applied exactly as the flush would.
            # _build_message_entry returns None for TRANSIENT roles
            # (chunk/done/streaming/queued/permission), so those are dropped and
            # never persisted by either the recovery write or the shutdown drain
            # — a `done` control row must not land in the JSONL transcript.
            out: "list[dict]" = []
            for r in rows:
                entry = _build_message_entry(r)
                if entry is not None:
                    out.append(entry)
            return out

        def _clear_pending_recovery() -> None:
            # Drop THIS regenerate's drain entry. Called the moment the reply is
            # durable again by any route — a new reply landed and the slot save
            # persisted it, the empty-turn restore spliced the old reply back and
            # committed, or _recover_to_original_transcript wrote it to the
            # original transcript — so the shutdown drain does not re-add an
            # already-recovered reply. Keyed by this regenerate's own entry id, so
            # it never clears a sibling regenerate's still-pending entry on the
            # same transcript.
            _forget_pending_recovery(recovery_entry_key)

        async def _recover_to_original_transcript(rows: "list[dict]") -> bool:
            # Durably write the removed reply BACK to its ORIGINAL transcript
            # (restore_expected_key) when the slot cannot host the restore
            # (rebound/replaced). AWAITS a confirmed, variants-and-metadata-
            # preserving write: returns True only once the rows are on the
            # original transcript's log, False if the write could not complete.
            # The caller must NOT drop or settle its in-memory recovery until
            # this returns True — a failed write would otherwise lose the only
            # recoverable copy of the reply. On False the caller keeps the
            # recovery pending and schedules a retry.
            if not rows or state.conversation_log is None:
                # Nothing to write (or no log): the recovery is vacuously
                # complete, so the caller may settle.
                return True
            canonical = _canonical_recovery_rows(rows)
            if not canonical:
                return True
            # Register the write in the shutdown-drain registry BEFORE awaiting
            # it, not only after it fails. The rows are already held out of the
            # slot window by the time any recovery write runs, so no periodic
            # flush can re-persist them; the ONLY thing that can finish the write
            # once this coroutine is suspended on disk I/O is the shutdown drain.
            # A gateway stop landing during this first await would otherwise find
            # nothing registered — the write in flight, the rows out of the slot,
            # the reply lost. Registering first makes every in-flight recovery
            # write (first attempt or retry) visible to the drain; the entry is
            # removed only on a CONFIRMED True, and retained on False so the
            # retry path and the drain both still see what is owed.
            #
            # Keyed by THIS regenerate's own entry id: a retry (or a fresh
            # attempt) within this regenerate REPLACES its own entry rather than
            # stacking, while a sibling regenerate on the same transcript keeps a
            # DISTINCT entry and is never clobbered. The stored transcript_key is
            # still restore_expected_key, so the drain writes to the right
            # transcript regardless of the entry id. On a confirmed write the
            # entry is dropped only if it is still THIS attempt's rows — a later
            # attempt that re-registered under the same id owns the entry then.
            _register_pending_recovery(
                recovery_entry_key,
                (
                    state.conversation_log,
                    restore_expected_key,
                    canonical,
                    transcript_existed_at_truncation,
                    transcript_deletion_generation,
                ),
            )
            committed = await restore_full_rows_off_loop(
                state.conversation_log,
                restore_expected_key,
                canonical,
                existed_at_truncation=transcript_existed_at_truncation,
                deletion_generation_at_truncation=transcript_deletion_generation,
            )
            if committed:
                existing = _PENDING_RECOVERIES.get(recovery_entry_key)
                if existing is not None and existing[2] is canonical:
                    _forget_pending_recovery(recovery_entry_key)
            return committed

        def _retry_recovery(rows: "list[dict]", attempt: int = 0) -> None:
            # Keep the recovery PENDING and retry the durable write out-of-band:
            # a confirmed write to the original transcript is the only thing that
            # makes it safe to settle, so a failure must leave the recovery
            # retryable rather than settled. The retry task INSTALLS itself as
            # slot._regenerate_restore_task so the restore-pending marker stays up
            # until a retry confirms the write — the marker is released only by a
            # settlement guard whose identity check sees this exact task as the
            # one on the slot.
            #
            # The recovery rows live ONLY in this closure's `rows` list and the
            # shutdown-drain registry, never in the rebound slot's window —
            # callers strip the spliced rows before scheduling a retry — so a
            # periodic flush between a failed write and the next retry cannot
            # persist them into the rebound conversation.
            #
            # Canonicalize ONCE here so the retry writes only canonical message
            # entries — a transient control row (a `done`/`chunk` that trailed
            # the reply in removed_rows) is dropped. The shutdown-drain registry
            # is owned by _recover_to_original_transcript, which registers each
            # in-flight write BEFORE awaiting it and removes it only on a
            # confirmed commit; so a gateway stop landing on this retry's write
            # (or on the first write before any retry) is visible to the drain
            # without this helper registering anything of its own.
            #
            # BOUNDED: a write that cannot EVER commit (a full or read-only data
            # home) must not re-arm forever at 4Hz. Each re-arm reinstalls
            # slot._regenerate_restore_task before _settle_retry runs, so an
            # unbounded loop would pin _regenerate_restore_pending permanently —
            # wedging _destructive_history_busy into a slot_restoring 409 on every
            # regenerate / edit-resend / switch-variant, making
            # _await_regenerate_restore always return False and _close_slot raise
            # forever. After _RECOVERY_DRAIN_ATTEMPTS the retry STOPS re-arming,
            # LEAVES the _PENDING_RECOVERIES entry for the shutdown drain (the
            # reply is still owed a durable write, so it must not be dropped), and
            # RELEASES the slot fences so ordinary operations are not blocked by a
            # recovery that can only ever be completed at shutdown or by a human
            # freeing disk.
            canonical_rows = _canonical_recovery_rows(rows)

            async def _again() -> None:
                await asyncio.sleep(_RECOVERY_RETRY_DELAY_SECS)
                if not await _recover_to_original_transcript(canonical_rows):
                    if attempt + 1 >= _RECOVERY_DRAIN_ATTEMPTS:
                        logger.warning(
                            "Regenerate: durable recovery of the previous reply to %s still "
                            "failing after %d attempts; leaving the entry for the shutdown drain "
                            "and releasing the slot fences rather than re-arming forever",
                            restore_expected_key,
                            attempt + 1,
                        )
                        # The entry stays in _PENDING_RECOVERIES (the drain still
                        # owes the write); release the fences so the slot is not
                        # wedged. Guarded on identity so a newer restore that
                        # replaced this task keeps its own fence up.
                        if slot._regenerate_restore_task is asyncio.current_task():
                            slot._regenerate_restore_task = None
                        if slot._regenerate_restore_task is None:
                            slot._regenerate_restore_pending = False
                        return
                    logger.warning(
                        "Regenerate: durable recovery of the previous reply to %s is still "
                        "failing; the slot stays restore-pending for a further retry",
                        restore_expected_key,
                    )
                    _retry_recovery(canonical_rows, attempt + 1)
                    return
                logger.info(
                    "Regenerate: durable recovery of the previous reply to %s committed on retry",
                    restore_expected_key,
                )
                state.push_slots_update()

            retry_task = asyncio.create_task(_again())

            def _settle_retry(done: asyncio.Task) -> None:
                # Mirror _clear_restore_task's identity guard: release the handle
                # and the restore-pending marker only when the task still on the
                # slot is the one that finished. A successor retry (scheduled by
                # _again above when the write failed again) replaces the handle
                # first, so this never clears the marker out from under a retry
                # that is still owed a confirmed write.
                if slot._regenerate_restore_task is done:
                    slot._regenerate_restore_task = None
                if slot._regenerate_restore_task is None:
                    slot._regenerate_restore_pending = False

            # Replace the restore task handle so the settlement guard keeps
            # _regenerate_restore_pending set until this retry (or a successor)
            # settles with a confirmed write.
            slot._regenerate_restore_task = retry_task
            state._background_tasks.add(retry_task)
            retry_task.add_done_callback(state._background_tasks.discard)
            retry_task.add_done_callback(_settle_retry)

        async def _restore_previous_reply(turn_reply_mids_snapshot: set[str]) -> None:
            # Re-validate the slot identity before touching the live slot or
            # disk. A same-name close-and-recreate (map holds another object) or
            # a rebind to a DIFFERENT transcript means this restore has no
            # authorized target on the LIVE slot. But a reconciler that binds an
            # unbound channel slot mid-turn only respells the key for the SAME
            # transcript file (bare <ts> / slack:<ts> / dashboard:slack_<ts>), so
            # compare by resolved transcript file, not the raw key string.
            if state._slots.get(name) is not slot or not _same_transcript(
                slot_history_key(slot), restore_expected_key
            ):
                # The slot is gone or now points at a DIFFERENT conversation, so
                # the restore cannot run on the live window. But the eager
                # truncation already removed the previous reply from the ORIGINAL
                # transcript (restore_expected_key) and persisted that removal, so
                # bailing here would leave that transcript permanently short of
                # its reply -- data loss on a conversation nobody rebound. Write
                # the captured FULL rows (variants + metadata preserved) back to
                # the transcript they came from, keyed by restore_expected_key
                # (not the slot's current key), and AWAIT the confirmation. This
                # is a durable-only recovery: the rows belong to a conversation
                # the live slot does not represent, so there is no window to
                # splice or client frame to broadcast -- only the archive to make
                # whole. The restore-pending marker stays set until the write is
                # confirmed, so a failed write keeps the slot retryable rather
                # than dropping the only recoverable copy.
                recovered = await _recover_to_original_transcript(removed_rows)
                if not recovered:
                    logger.warning(
                        "Regenerate: slot %s rebound/replaced before restore and the durable "
                        "write of the previous reply to its original transcript (%s) did not "
                        "commit; retrying rather than dropping the only recoverable copy",
                        slot.key,
                        restore_expected_key,
                    )
                    _retry_recovery(removed_rows)
                    return
                logger.warning(
                    "Regenerate: slot %s was rebound to another transcript or replaced before "
                    "the reply could be restored; wrote the previous reply back to its original "
                    "transcript (%s) rather than leaving that conversation short of its reply",
                    slot.key,
                    restore_expected_key,
                )
                return

            # The restore only makes sense when the turn produced NO reply. The
            # unconsumed variant stash is NOT that signal: _flush_segment clears
            # it, but the abnormal-exit paths (kiro-cli died mid-stream, user
            # Stop) persist their partial text through _persist_partial_reply,
            # which appends a real assistant reply row WITHOUT going through
            # _flush_segment, leaving the stash set. Restoring on top of such a
            # partial reply would commit user -> OLD reply -> NEW partial reply
            # for one turn.
            #
            # Detect the turn's reply by IDENTITY, not row shape. The live window
            # is shared: a workflow completion (workflow_inject) or a cron result
            # (cron_inject) is appended into it as an assistant "msg msg-a" row by
            # another writer while the turn runs, and neither is a system notice,
            # so a scan for any assistant "msg-a" row reads that foreign row as
            # this turn's reply and drops the restore -- losing the pre-regenerate
            # reply for good. _turn_reply_mids holds the message-ids of the reply
            # rows THIS turn appended (filled by _note_reply_row from both
            # _flush_segment and _persist_partial_reply, so a partial reply still
            # counts); a row whose mid is not in that set is not this turn's reply.
            #
            # Read the SNAPSHOT taken when this turn finished, not the live
            # attribute. _run_chat clears slot._turn_reply_mids at the top of
            # every turn, and a queued follow-up's turn can run that clear before
            # this scheduled restore does -- so re-reading the live list here
            # would see an empty set and let a partial reply through, persisting
            # user -> OLD reply -> PARTIAL reply. The snapshot is captured in the
            # done-callback below, before the successor can be dispatched.
            if restore_anchor_mid:
                anchor_pos = next(
                    (i for i, r in enumerate(slot.messages) if row_mid(r) == restore_anchor_mid),
                    None,
                )
            else:
                anchor_pos = None
            # Where to start scanning for this turn's reply. When the anchor row
            # is still in the window, scan after it. When it is NOT -- a long
            # streamed regenerate can front-trim the user row out of the window
            # entirely, or the row carried no mid -- scanning from the stale
            # pre-dispatch u_idx is WRONG: the trim shifted every surviving row
            # down, so a partial reply the turn produced now sits BEFORE u_idx,
            # the membership scan misses it, and the guard wrongly concludes "no
            # reply" and splices the old reply AFTER the partial rows (corrupted
            # order). A front-trim only removes from the FRONT, so once the
            # anchor is gone every row still in the window arrived at or after it
            # -- the same reasoning _turn_rows uses for a trimmed turn-start row.
            # Scan the whole surviving window (0) in that case; membership in
            # turn_reply_mids_snapshot is what actually gates, and a wider scan
            # can only find MORE of THIS turn's own reply rows, never a foreign
            # or pre-regenerate row (whose mid is not in the snapshot).
            _scan_from = (anchor_pos + 1) if anchor_pos is not None else 0
            partial_reply_row = next(
                (
                    r
                    for r in reversed(slot.messages[_scan_from:])
                    if row_mid(r) in turn_reply_mids_snapshot and row_mid(r)
                ),
                None,
            )
            if partial_reply_row is not None:
                # The turn produced a (partial) reply — do NOT splice the old
                # reply on top of it. But the old reply must not simply vanish:
                # the eager truncation deleted it and the done-callback cleared
                # _pending_variants, so without this its variant history is lost
                # from both the transcript and the variant chain. Attach the old
                # reply (and its prior variants) to the partial reply as variant
                # history, exactly as _flush_segment would have for a normal
                # reply, so the user can switch back to it. Dedup by content,
                # keep the partial as the active (newest) variant, cap the list.
                existing = partial_reply_row.get("variants")
                merged: list[dict] = list(existing) if isinstance(existing, list) else []
                partial_entry = variant_from_row(partial_reply_row)
                # Snapshot the row's pre-attach variant state so a rebind during
                # the save-await below can be rolled back: the attached variants
                # carry the OLD conversation's content, which must not ride a
                # rebind into a newly-linked transcript.
                _pre_variants = copy.deepcopy(existing) if isinstance(existing, list) else None
                _pre_variant_idx = partial_reply_row.get("variant_idx")
                _pre_content = partial_reply_row.get("content")
                for old_variant in removed_variants:
                    if not isinstance(old_variant, dict):
                        continue
                    if not any(v.get("content") == old_variant.get("content") for v in merged):
                        merged.append(old_variant)
                # ALWAYS append the partial as the newest variant before capping,
                # mirroring _flush_segment, which appends its reply unconditionally.
                # A content dedup here would skip the append when the partial
                # matched an older merged entry, yet variant_idx is still set to
                # len(merged) - 1 below — so the active index would point at that
                # OLDER entry instead of the partial, and the row's adopted text
                # (the partial) would disagree with the variant the index names.
                # Appending unconditionally keeps the partial as the last element,
                # so len(merged) - 1 always names it.
                merged.append(partial_entry)
                if len(merged) > _MAX_VARIANTS:
                    merged = merged[-_MAX_VARIANTS:]
                adopt_variant_text(partial_reply_row, partial_entry)
                partial_reply_row["variants"] = merged
                partial_reply_row["variant_idx"] = len(merged) - 1
                slot.invalidate_source_links()
                slot._dirty = True
                slot._resumed_count = 0
                committed = False
                try:
                    committed = await save_slot_off_loop(
                        state,
                        slot,
                        best_effort=False,
                        expected_history_key=slot_history_key(slot),
                        expected_slot_name=name,
                        # When a tab close drives this restore, the close itself
                        # has already set is_closing and AWAITS this save before
                        # archiving (via _await_regenerate_restore). The save
                        # guard's is_closing fence exists to refuse a NEW write
                        # that races a retraction's own drain -- but this write IS
                        # that drain's write, sequenced by the close, and is the
                        # last chance the restored reply has to reach disk in
                        # order. Declare it so the guard lets it through; a
                        # refusal here would send the else-branch to write the old
                        # reply as a stray TOP-LEVEL row that the archival merge
                        # then interleaves out of order.
                        issued_by_the_retraction=getattr(slot, "is_closing", False),
                    )
                except Exception:
                    logger.warning(
                        "Regenerate: failed to persist variants onto the partial reply for %s",
                        slot.key,
                        exc_info=True,
                    )
                # Re-verify transcript identity on EVERY outcome branch — commit
                # success, refusal, or exception — the mirror of the empty-restore
                # path. A mid-await rebind can leave the save committed=True
                # against the OLD transcript while the slot now links a NEW one;
                # the attached variants carry the old conversation's reply, so
                # leaving them on the row would let the next unpinned periodic
                # flush persist it into the newly-linked transcript. Checked
                # before the commit/not-commit split so the committed-true branch
                # cannot skip it. Roll the row back to its pre-attach snapshot on
                # a real rebind (not a mere key respelling onto the same file).
                if not _same_transcript(slot_history_key(slot), restore_expected_key):
                    # The old conversation's reply lives in removed_rows, and the
                    # revert below drops it from this (rebound) slot. Revert the
                    # partial row's attached variants/index/content BEFORE the
                    # recovery await, not after: the await suspends while the slot
                    # is already bound to the NEW conversation, and leaving the
                    # old conversation's variants on partial_reply_row across that
                    # suspension lets an unpinned periodic flush persist them into
                    # the newly-linked transcript (a cross-transcript variant
                    # leak). Stripping first means the dirty slot carries nothing
                    # of the old conversation while recovery is in flight. The
                    # rows survive in removed_rows (a separate deep-copy), so the
                    # revert loses no recoverable content. This mirrors the
                    # identity-rollback the commit/refuse branches already do.
                    if _pre_variants is None:
                        partial_reply_row.pop("variants", None)
                    else:
                        partial_reply_row["variants"] = _pre_variants
                    if _pre_variant_idx is None:
                        partial_reply_row.pop("variant_idx", None)
                    else:
                        partial_reply_row["variant_idx"] = _pre_variant_idx
                    if _pre_content is not None:
                        partial_reply_row["content"] = _pre_content
                    slot.invalidate_source_links()
                    # Now durably recover the old reply to the ORIGINAL transcript,
                    # confirmed, keeping it retryable on a failed write. The rows
                    # are already off the rebound slot, so a flush landing between
                    # a failed write and a retry has nothing to leak.
                    recovered = await _recover_to_original_transcript(removed_rows)
                    if not recovered:
                        _retry_recovery(removed_rows)
                    logger.warning(
                        "Regenerate: slot %s was rebound to another transcript during the "
                        "variant attach (committed=%s); reverted the partial reply's variants so "
                        "the previous conversation's reply cannot leak into the newly-linked "
                        "transcript",
                        slot.key,
                        committed,
                    )
                    state.push_slots_update()
                    return
                if committed:
                    # The old reply is now durable as a variant of the partial
                    # reply on the (same) original transcript, so the pre-turn
                    # drain entry is already satisfied — drop it.
                    _clear_pending_recovery()
                    _bc, _ = redact_exfiltration_urls(partial_reply_row.get("content", ""))
                    _bc, _ = redact_credentials(_bc)
                    state.broadcast_ws(
                        "chat_variant_switch",
                        {"slot": slot.key, "index": len(merged) - 1, "content": _bc},
                    )
                else:
                    # The attach save did not commit on the SAME transcript (a
                    # transient lock timeout / I/O error, or a key respelling the
                    # save guard rejected). Two durability routes would otherwise
                    # both claim the old reply and collide: the dirty slot's
                    # periodic flush would persist it as a VARIANT nested in the
                    # partial reply, while the pre-turn drain entry would write it
                    # as a TOP-LEVEL row — and the top-level write is NOT deduped
                    # against a nested variant (append_full_message_if_absent
                    # scans top-level rows by mid), so a later drain would
                    # DUPLICATE the reply. There is also no general dirty-slot
                    # flush at shutdown, so the nested-variant route is not even a
                    # guaranteed durable copy. Make the drain entry the SINGLE
                    # owner: revert the dirty variant attach to its pre-attach
                    # snapshot (so the periodic flush persists no second copy) and
                    # recover the old reply to the original transcript via the
                    # confirmed recovery path, which clears the entry on success
                    # and retries on failure — exactly the sibling rebind branch's
                    # discipline, minus the rebind.
                    if _pre_variants is None:
                        partial_reply_row.pop("variants", None)
                    else:
                        partial_reply_row["variants"] = _pre_variants
                    if _pre_variant_idx is None:
                        partial_reply_row.pop("variant_idx", None)
                    else:
                        partial_reply_row["variant_idx"] = _pre_variant_idx
                    if _pre_content is not None:
                        partial_reply_row["content"] = _pre_content
                    slot.invalidate_source_links()
                    recovered = await _recover_to_original_transcript(removed_rows)
                    if not recovered:
                        _retry_recovery(removed_rows)
                    logger.warning(
                        "Regenerate: variant attach for %s was not committed (transient failure "
                        "or key respelling on the SAME transcript); reverted the attach and "
                        "recovered the previous reply to its original transcript so the drain "
                        "cannot later duplicate it",
                        slot.key,
                    )
                    state.push_slots_update()
                logger.info(
                    "Regenerate: turn produced a (possibly partial) reply for %s; attached "
                    "the previous reply as a variant rather than restoring on top of it",
                    slot.key,
                )
                return

            # Recompute the insertion point at restore time. u_idx is a numeric
            # position captured before the turn dispatched; the turn appends rows
            # (chunk rows count toward _MAX_SLOT_MESSAGES until the turn-end
            # purge), so a front-trim during the turn can shift every position
            # down and leave u_idx pointing past the user row -- splicing there
            # would drop the reply AFTER an error/status row, corrupting order.
            # Relocate the user row by its stable mid and insert right after it,
            # so the transcript reads user -> reply -> whatever followed (an
            # error card, a workflow/sub-agent injection, or a QUEUED FOLLOW-UP's
            # user row that _start_next_queued_turn appended from this turn's
            # tail-drain), in chronological order. Restoring at the relocated
            # user row rather than bailing on a later user row is what keeps a
            # follow-up sent during the regenerate from silently deleting the
            # reply. _pending_variants was already cleared before this task was
            # scheduled, so a queued successor turn's flush cannot attach the old
            # reply's variants to the follow-up's reply.
            insert_at = (anchor_pos + 1) if anchor_pos is not None else None
            if insert_at is None:
                # The anchor row is gone (front-trimmed off the window entirely,
                # or never carried a mid): fall back to the captured index,
                # clamped into range so a shrunk window cannot raise.
                insert_at = min(u_idx + 1, len(slot.messages))

            # Install the removed rows on the LIVE window. Spliced (not
            # re-appended) so each row keeps its own meta.mid and the reply keeps
            # its variant history; a fresh append would mint new ids and drop the
            # variants. Mutating the live window -- rather than persisting a
            # frozen snapshot -- is what lets the ordinary merge save below carry
            # BOTH the rows already present here AND any cross-process disk
            # append that lands during the write: a rewrite-path save (forced by
            # an explicit snapshot) disables that foreign-row scan and would drop
            # such an append to the archive.
            restored = copy.deepcopy(removed_rows)
            slot.messages[insert_at:insert_at] = restored
            # The splice bypasses append, so it also bypasses append's own cap
            # enforcement; run the shared bound helper so the restored rows
            # cannot ratchet the window past _MAX_SLOT_MESSAGES over repeated
            # empty regenerations, with the same trim-counter bookkeeping append
            # applies.
            slot._enforce_message_bound()
            # Bound enforcement front-trims the window, and with a near-cap window
            # plus a trailing notice it can trim the JUST-SPLICED reply back off
            # before the save sees it — the reply would then be permanently
            # omitted (never persisted) while the broadcast still shipped it.
            # Split the restored rows into those that SURVIVED the trim (still in
            # the live window by identity) and those EVICTED by it. The survivors
            # ride the ordinary merge save below. The evicted rows are durably
            # written to the transcript directly, confirmed, so the reply is
            # retained on disk even though the live window could not hold it; and
            # only survivors are broadcast, so no frame announces a row absent
            # from the committed transcript.
            _live_ids = {id(m) for m in slot.messages}
            restored_survivors = [r for r in restored if id(r) in _live_ids]
            restored_evicted = [r for r in restored if id(r) not in _live_ids]
            # True when bound enforcement evicted rows AND their durable recovery
            # did NOT commit: _recover_to_original_transcript then left (or
            # _retry_recovery re-registered) a pending entry under the SHARED
            # restore_expected_key holding those evicted rows, owed to a retry or
            # the shutdown drain. The survivor-save pop below must not clear THAT
            # entry — it is not the pre-turn entry and dropping it loses the
            # evicted rows. Default False (no evicted rows / recovery committed →
            # the key holds only the pre-turn entry, safe to clear on commit).
            evicted_recovery_pending = False
            if restored_evicted:
                # Persist the trimmed rows to the (same) original transcript
                # before the save, confirmed, so bound enforcement cannot drop
                # the reply. These rows left the live window, so the merge save
                # will not carry them; the direct write is their only durable
                # home. _recover_to_original_transcript builds the canonical
                # entry and honors a concurrent delete.
                evicted_ok = await _recover_to_original_transcript(restored_evicted)
                if not evicted_ok:
                    evicted_recovery_pending = True
                    logger.warning(
                        "Regenerate: bound enforcement evicted %d restored row(s) for %s and the "
                        "durable write to the original transcript (%s) did not commit; retrying "
                        "so the trimmed reply is not lost",
                        len(restored_evicted),
                        slot.key,
                        restore_expected_key,
                    )
                    _retry_recovery(restored_evicted)
            slot.invalidate_source_links()
            slot._dirty = True
            slot._resumed_count = 0
            # The window grew back past the truncation. Clear the rewrite flag
            # the eager truncation armed so this save takes the ORDINARY merge
            # path (collect_foreign on), which preserves a concurrent
            # sub-agent/cron/CLI disk append instead of a rewrite dropping it.
            slot._pending_rewrite = False

            # Persist the LIVE slot before broadcasting -- persist-before-publish
            # for the visible frames -- through the ordinary merge path
            # (messages omitted, rewrite off). best_effort=False so a lock
            # timeout / I/O error propagates as committed=False instead of being
            # swallowed and reported as a commit; the two guards refuse the write
            # if the slot was rebound, replaced, or is closing while it awaited
            # its lock. On any non-commit the restored rows stay in the live
            # window with _dirty set, so the periodic merge flush retries the
            # durable write -- the window is the source of truth -- but the
            # client frames are NOT sent, so no unsaved restore is announced.
            committed = False
            try:
                committed = await save_slot_off_loop(
                    state,
                    slot,
                    best_effort=False,
                    # Pin to the slot's CURRENT key. _same_transcript above
                    # confirmed it resolves to the same file as the captured
                    # key, but a mid-turn rebind may have respelled it; the save
                    # guard compares the raw string, so it must be handed the
                    # spelling the slot now routes to, not the stale one.
                    expected_history_key=slot_history_key(slot),
                    expected_slot_name=name,
                    # A tab close drives this restore and AWAITS it before
                    # archiving, so the save guard's is_closing fence must let
                    # this (the retraction's own) write through; otherwise the
                    # restored rows stay dirty-only and the close archives the
                    # truncated transcript, losing the reply.
                    issued_by_the_retraction=getattr(slot, "is_closing", False),
                )
            except Exception:
                logger.warning(
                    "Regenerate: failed to persist the restored reply for %s",
                    slot.key,
                    exc_info=True,
                )

            # Re-verify transcript identity after the save-await on EVERY outcome
            # branch — commit success, refusal, OR exception. A cron/workflow can
            # bind this (unbound) slot to a DIFFERENT conversation during the
            # await; the save's own pin matched when it ran (so it can even return
            # committed=True against the OLD transcript), but the slot now links a
            # new one. Leaving the restored rows on it would let the next UNPINNED
            # periodic flush persist the previous conversation's reply into the
            # newly-linked transcript — cross-conversation corruption. Checked
            # BEFORE the commit/not-commit split so neither branch can skip it:
            # the committed-true branch is the mirror hazard of the refused one.
            # Remove exactly the rows this restore spliced (by object identity) so
            # nothing of the old conversation rides the rebind. A mere key
            # respelling onto the SAME file is NOT a rebind (_same_transcript true)
            # and is left alone.
            if not _same_transcript(slot_history_key(slot), restore_expected_key):
                # The slot rebound to a DIFFERENT conversation during the save.
                # The spliced rows carry the PREVIOUS conversation's reply; the
                # restore recovers them to the ORIGINAL transcript. Remove them
                # from the rebound slot IMMEDIATELY — not after recovery commits —
                # because a periodic flush can land between a failed recovery
                # write and a later retry, and any recovery rows left in
                # slot.messages would be persisted into the NEW transcript
                # (cross-conversation corruption). The rows survive in the
                # recovery closure's own list (removed_rows is a separate
                # deep-copy), so stripping the live splice loses nothing. A mere
                # key respelling onto the SAME file is NOT a rebind
                # (_same_transcript true) and never reaches here.
                _restored_ids = {id(r) for r in restored}
                slot.messages[:] = [m for m in slot.messages if id(m) not in _restored_ids]
                slot.invalidate_source_links()
                # Now recover to the ORIGINAL transcript, confirmed. The rows are
                # already out of the rebound slot, so a failure simply retries the
                # durable write — there is nothing left in the slot for a flush to
                # leak. The fence stays up (via the retry task) until the write
                # commits.
                recovered = await _recover_to_original_transcript(removed_rows)
                if not recovered:
                    logger.warning(
                        "Regenerate: slot %s rebound during the restore save and the durable "
                        "write of the previous reply to its original transcript (%s) did not "
                        "commit; the rows are held out of the rebound slot and the write is "
                        "retried rather than dropped",
                        slot.key,
                        restore_expected_key,
                    )
                    _retry_recovery(removed_rows)
                    return
                logger.warning(
                    "Regenerate: slot %s was rebound to another transcript during the restore "
                    "save (committed=%s); stripped the restored rows off the rebound slot and "
                    "recovered the previous reply to its original transcript (%s)",
                    slot.key,
                    committed,
                    restore_expected_key,
                )
                state.push_slots_update()
                return
            if not committed:
                # Same transcript, but the write did not commit (a transient lock
                # timeout / I/O error, or a key respelling the save guard rejected
                # by raw-string compare). Keep the restored rows in the live
                # window with _dirty set so the periodic merge flush retries the
                # durable write onto the RIGHT conversation; broadcast nothing, so
                # no unsaved restore is announced.
                logger.warning(
                    "Regenerate: persist of the restored reply for %s was not committed "
                    "(transient failure or key respelling on the SAME transcript); the reply is "
                    "held in the live window (dirty) for the periodic flush to retry, and no "
                    "frames were broadcast",
                    slot.key,
                )
                state.push_slots_update()
                return

            # Durable copy exists. Reverse the client's optimistic truncation:
            # the endpoint returned 200, so ChatPage's optimistic
            # truncateAfterIndex stands and the open tab still shows the reply as
            # deleted; neither push_slots_update (slot list only) nor chat_done
            # refetches the transcript on its own here. Ship each restored row
            # through the canonical _broadcast_chat_message door -- the SAME one
            # append uses -- so content and meta go through the identical
            # display-redaction + allowed-link + parse_cls_meta passes the live
            # stream applies (a hand-built frame skipped those, so a stop card's
            # cls-only kind and a workspace-allowed link came out wrong).
            # Broadcast only the rows that SURVIVED bound enforcement and are in
            # the committed window — never a row the trim evicted (those were
            # written to disk directly above, but are not part of the live
            # window the client refetch will see).
            #
            # The reply is durable on the (same) original transcript now — the
            # merge save committed and no rebind intervened — so the pre-turn
            # drain entry is already satisfied: drop it so a shutdown drain does
            # not re-write an already-restored reply. (Evicted rows went to disk
            # via _recover_to_original_transcript, which manages the same key.)
            #
            # But NOT when an evicted-rows recovery is still pending: it was
            # registered under the SHARED restore_expected_key and is owed to a
            # retry or the drain, so clearing here — unlike the committed case —
            # would drop the evicted rows (the recurring gap its sibling pop does
            # not have). Leave the entry; _recover_to_original_transcript's own
            # commit on a successful retry clears it.
            if not evicted_recovery_pending:
                _clear_pending_recovery()
            for row in restored_survivors:
                state._broadcast_chat_message(slot.key, row)
            # The per-row chat_message frames are append-shaped, so a mid-window
            # restore (the reply precedes an error card / follow-up already on
            # screen, or a successor turn is streaming during the save await)
            # can render out of order or clobber a live bubble. Emit one
            # chat_variant_switch: the client handler fires refreshSlot(slot)
            # unconditionally on it, which re-fetches the authoritative
            # transcript and puts every row in its committed order. The index is
            # best-effort for viewers that render the field directly.
            _reply = (
                restored_survivors[0] if restored_survivors else (restored[0] if restored else {})
            )
            _reply_variants = _reply.get("variants") if isinstance(_reply, dict) else None
            state.broadcast_ws(
                "chat_variant_switch",
                {
                    "slot": slot.key,
                    "index": (
                        _reply.get("variant_idx", len(_reply_variants) - 1)
                        if isinstance(_reply_variants, list) and _reply_variants
                        else 0
                    ),
                    "content": (
                        redact_display_content(_reply.get("content", ""))
                        if isinstance(_reply, dict)
                        else ""
                    ),
                },
            )

            logger.info(
                "Regenerate: turn produced no reply for %s; restored the previous reply",
                slot.key,
            )
            state.push_slots_update()

        def _clear_pending_on_done(t: asyncio.Task) -> None:
            # Variants still pending here means _flush_segment never ran, so the
            # regenerated turn produced no assistant reply. The truncation above
            # already removed the previous reply from the window AND persisted
            # the truncated transcript, so simply clearing the variants would
            # destroy the only surviving copy of the reply the user asked to
            # improve on. Schedule the restore, which splices the removed rows
            # back into the dirty live window, persists them, and broadcasts the
            # recovered rows to the client only after that write commits.
            if not slot._pending_variants:
                # A reply landed and _flush_segment consumed the variants (and
                # already cleared the restore-pending marker). The new reply and
                # the old reply (now its attached variant) are on the LIVE window
                # ONLY — _flush_segment does not itself persist them, and
                # slot.append is in-memory — so dropping the drain entry NOW would
                # be popping before confirming the replacement is durable. A
                # transient save failure plus a correlated gateway stop would then
                # lose BOTH replies from an already-truncated transcript, with no
                # registered entry for the drain to flush. Instead, confirm a
                # committed save FIRST, then pop — mirroring the drain's own
                # commit-confirmed-then-pop discipline; on a non-committing save
                # the entry is RETAINED so the drain still has the old reply.
                async def _confirm_replacement_then_clear() -> None:
                    committed = False
                    try:
                        committed = await save_slot_off_loop(
                            state,
                            slot,
                            best_effort=False,
                            expected_history_key=slot_history_key(slot),
                            expected_slot_name=name,
                            # When a tab close is what settled this turn, this
                            # confirmation is the retraction's own drain write —
                            # the close awaits it before archiving. The is_closing
                            # fence must let it through so the replacement commits
                            # and the pre-turn recovery entry is POPPED here; a
                            # refusal would retain the entry, and each close would
                            # then leak one entry toward the 512-entry cap (the
                            # shutdown drain re-inserts it).
                            issued_by_the_retraction=getattr(slot, "is_closing", False),
                        )
                    except Exception:
                        logger.warning(
                            "Regenerate: confirming the replacement save for %s raised; the "
                            "pre-turn recovery entry is retained for the shutdown drain",
                            slot.key,
                            exc_info=True,
                        )
                    # Only drop the drain entry once the replacement (new reply +
                    # its attached old-reply variant) is durable on the SAME
                    # transcript the truncation was authorized against. A rebind
                    # during the await leaves the replacement on a different
                    # transcript, so the old reply still owes a recovery to its
                    # original — keep the entry for the drain in that case too.
                    if committed and _same_transcript(slot_history_key(slot), restore_expected_key):
                        _clear_pending_recovery()

                confirm_task = asyncio.create_task(_confirm_replacement_then_clear())
                state._background_tasks.add(confirm_task)
                confirm_task.add_done_callback(state._background_tasks.discard)
                # Let the shutdown drain settle this confirmation before it writes
                # the fallback entry: the confirmation persists the replacement and
                # drops the entry, so draining ahead of it would duplicate the reply.
                _gate = _PENDING_RECOVERY_GATES.get(recovery_entry_key)
                if _gate is not None:
                    _gate.settle = confirm_task
                slot._regenerate_restore_pending = False
                return
            slot._pending_variants = []
            if not removed_rows:
                # Nothing to put back, so no restore task is scheduled: release
                # the marker here or the slot reads busy forever.
                _clear_pending_recovery()
                slot._regenerate_restore_pending = False
                return
            # Use ONLY the value captured at the _run_chat return boundary
            # (above). A queued follow-up's turn can have cleared
            # slot._turn_reply_mids before this callback runs, so reading the
            # live list here would see an empty set and splice the old reply on
            # top of a partial reply.
            turn_reply_mids_snapshot = captured_reply_mids.get("mids", set())
            restore_task = asyncio.create_task(_restore_previous_reply(turn_reply_mids_snapshot))
            # Tracked so a test (and a shutdown) can await the restore's real
            # completion rather than polling, and so it is not garbage-collected
            # mid-flight. _destructive_history_busy reads this handle to fence a
            # second mutation out of the restore's splice-then-save window, so it
            # must return to None once the restore settles — otherwise a
            # completed restore would wedge the slot. Reset on EVERY exit
            # (success, failure, cancellation) and only when this exact task is
            # still the one on the slot, so a later restore that replaced it is
            # not cleared out from under itself.
            slot._regenerate_restore_task = restore_task
            state._background_tasks.add(restore_task)
            restore_task.add_done_callback(state._background_tasks.discard)
            # Let the shutdown drain settle this restore before it writes the
            # fallback entry: the restore splices the reply back and persists it
            # (dropping the entry on commit via _recover_to_original_transcript or
            # the slot save), so draining ahead of it would duplicate the reply.
            _restore_gate = _PENDING_RECOVERY_GATES.get(recovery_entry_key)
            if _restore_gate is not None:
                _restore_gate.settle = restore_task

            def _clear_restore_task(done: asyncio.Task) -> None:
                if slot._regenerate_restore_task is done:
                    slot._regenerate_restore_task = None
                # The restore has fully settled: release the restore-pending
                # marker that has fenced teardown across the whole window. Guarded
                # on identity so a newer restore that replaced this one keeps the
                # marker up for itself.
                if slot._regenerate_restore_task is None:
                    slot._regenerate_restore_pending = False

            restore_task.add_done_callback(_clear_restore_task)

        # Register the removed reply as a PENDING recovery NOW — synchronously
        # after the truncating write committed above and before the turn runs,
        # not only once the done-callback decides a restore is needed. The
        # truncation has already persisted the ORIGINAL transcript short of its
        # reply, and the reply lives only in `removed_rows` (an in-memory
        # closure) for the WHOLE turn; a gateway stop during the turn — an
        # in-flight regenerate plus a stop, both ordinary — would otherwise leave
        # the original durably truncated with the recovery registered nowhere, so
        # the shutdown drain could not flush it. Registering here makes the reply
        # drain-visible across the entire window between the truncation and
        # whichever terminal path makes it durable again, at which point
        # _clear_pending_recovery drops it (a reply landed and the slot save
        # persisted it, the restore spliced it back and committed, or
        # _recover_to_original_transcript wrote it to the original transcript).
        # Keyed by THIS regenerate's own entry id, so a SECOND regenerate on the
        # same transcript (admitted once this one's fence releases) registers a
        # DISTINCT entry and cannot overwrite this one's still-pending rows — the
        # overwrite that would otherwise silently lose the first reply. A deleted/
        # absent transcript is honored by restore_full_rows_off_loop (returns
        # True, not recreated), so a drain of this entry never resurrects a
        # conversation the user deleted.
        _pre_turn_recovery = _canonical_recovery_rows(removed_rows)
        if state.conversation_log is not None and _pre_turn_recovery:
            _register_pending_recovery(
                recovery_entry_key,
                (
                    state.conversation_log,
                    restore_expected_key,
                    _pre_turn_recovery,
                    transcript_existed_at_truncation,
                    transcript_deletion_generation,
                ),
            )
            # Gate the drain on THIS regenerate's turn task and (once the done-
            # callback below creates it) its attach/confirm/restore task, so a
            # shutdown mid-regenerate settles that task before draining this
            # entry rather than writing the reply the task is also persisting.
            _PENDING_RECOVERY_GATES[recovery_entry_key] = _RecoverySettleGate(task)
        # The reservation has reached its terminal point: it is now a real
        # counted registry entry (above) or there was nothing to register.
        # Either way release the reservation so admission counts the LIVE entry
        # (not entry-plus-reservation) and a never-registered reservation does
        # not permanently narrow capacity. Released here, synchronously, before
        # any further await.
        _release_recovery_reservation()

        task.add_done_callback(_clear_pending_on_done)
    state.push_slots_update()
    return web.json_response({"ok": True})


async def api_chat_slot_switch_variant(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/switch-variant — switch which regenerated variant is active."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    under_construction = reject_if_slot_under_construction(state, slot)
    if under_construction is not None:
        return under_construction

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    try:
        idx = int(body.get("index"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid index", "code": "index_invalid"}, status=400)

    async with slot._lock:
        busy = _destructive_history_busy(slot)
        if busy is not None:
            return busy

        target = None
        for m in reversed(slot.messages):
            if m.get("role") == "assistant" and m.get("variants"):
                target = m
                break
        if target is None:
            return web.json_response({"error": "no variants", "code": "no_variants"}, status=400)
        raw_target_variants = target.get("variants")
        variants: list[dict] = (
            list(raw_target_variants)  # type: ignore[arg-type]
            if isinstance(raw_target_variants, list)
            else []
        )
        if idx < 0 or idx >= len(variants):
            return web.json_response(
                {"error": "index out of range", "code": "index_out_of_range"}, status=400
            )

        chosen = variants[idx]
        if not isinstance(chosen, dict):
            return web.json_response(
                {"error": "corrupt variant entry", "code": "variant_corrupt"}, status=400
            )
        target_dict: dict = target
        adopt_variant_text(target_dict, chosen)
        slot.invalidate_source_links()
        target_dict["variant_idx"] = idx
        slot._dirty = True
        slot._resumed_count = 0
        # Same two-axis pin as regenerate above, matching the pair edit-resend
        # carries: routing (save_slot_off_loop refuses when the slot resolves to
        # a different transcript at write time -- a renamed replacement) and
        # object identity (expected_slot_name carries this slot's map key into
        # the save, where state._slots[name] is re-read at the locked commit
        # boundary with no await before the write -- a same-name recreate that
        # resumes the same transcript keeps the key identical, so only the
        # identity check catches it, and it must run at the write not before it
        # because the recreate can land inside the executor wait). best_effort
        # re-arms _dirty on a transient failure so the periodic flush retries.
        expected_history_key = slot_history_key(slot)
        try:
            msgs_snapshot = list(slot.messages)
            committed = await save_slot_off_loop(
                state,
                slot,
                msgs_snapshot,
                expected_history_key=expected_history_key,
                expected_slot_name=name,
            )
        except Exception:
            logger.warning("switch-variant: failed to persist", exc_info=True)
            committed = True
        if not committed:
            # The save's guards refused: the slot was rebound or a same-name
            # recreate replaced it while the write awaited its lock. The chosen
            # variant exists only in this popped slot's in-memory window;
            # broadcasting the switch would announce a state no transcript holds,
            # so abort without broadcasting.
            logger.warning(
                "switch-variant: history save refused for %s (concurrent delete or recreate)",
                slot.key,
            )
            return web.json_response(
                {
                    "error": "the conversation changed while saving; retry",
                    "code": "switch_variant_save_refused",
                },
                status=409,
            )
        sel().log_api_access(
            caller="dashboard",
            operation="chat.switch_variant",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )
        _bc, _ = redact_exfiltration_urls(target_dict["content"])
        _bc, _ = redact_credentials(_bc)
        state.broadcast_ws(
            "chat_variant_switch",
            {"slot": slot.key, "index": idx, "content": _bc},
        )
        return web.json_response({"ok": True, "index": idx})


async def api_chat_slot_edit_resend(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/edit-resend — edit a user message and resend."""
    # Local import, and it must STAY local: ``chat_handlers`` cannot be the first
    # module of the package to import (its own transitive
    # ``validation`` <-> ``artifacts`` cycle resolves only once something else
    # has pulled those in), so hoisting these two to module scope makes
    # ``import kiro_crew.dashboard.chat_regenerate`` fail on its own. Same reason
    # ``session_control`` and ``handlers/core`` reach it this way.
    from kiro_crew.dashboard.chat_handlers import (
        _check_slot_app_ownership,
        _reauthorize_after_await,
        _subagents_attached_response,
    )

    # Destructive: this truncates and PERSISTS history before the background
    # turn runs, so a failed turn cannot undo it. Unlike an ordinary send, the
    # readiness latch must be honored BEFORE the mutation.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    under_construction = reject_if_slot_under_construction(state, slot)
    if under_construction is not None:
        return under_construction

    # App-ownership gate (App Kit §5.2). This endpoint discards the slot's
    # NATIVE ACP conversation below, so an app token reaching a slot it does not
    # own destroys a resume identity it has no claim on -- the same capability
    # every other app-reachable write authorizes first. Reuse the shared gate
    # rather than a second spelling of it: it authorizes all four keys
    # (``_app`` presence, ``_app`` match, the effective SESSION key, and the
    # TRANSCRIPT key), so a channel-linked slot -- whose effective session is a
    # conversation the app does not own -- and an UNBOUND channel-origin slot
    # are both already covered, with no separate link check to keep in sync.
    # Denials are 404, not 403: indistinguishable from a missing slot
    # (anti-enumeration, CWE-204); the true reason is logged via SEL inside.
    denied = _check_slot_app_ownership(slot, name, request_app, "chat.slot_edit_resend")
    if denied is not None:
        return denied

    # A crew-bound slot has no local edit-and-resend: it would truncate LOCAL
    # history and re-run the edited turn on this machine, diverging from the peer.
    # AFTER the app-ownership 404 above so a foreign app cannot tell a remote slot
    # apart from a missing one via the 409.
    refusal = remote_bound_refusal(slot)
    if refusal is not None:
        return refusal

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # A valid-JSON but non-object body (array/scalar) has no .get(), so
    # body.get("index") would raise AttributeError -> 500. Reject it as a 400,
    # matching the guard in api_chat_slot_switch_variant above.
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    index = body.get("index")
    ts = body.get("ts")
    # A PRESENT non-string ``content`` (``{"content": 123}``) has no ``.strip()``,
    # so it reached ``AttributeError`` -> 500 rather than a 400 the caller can
    # read. Missing/null stays ``content_required`` below, which is what an empty
    # composer sends. Both checks mirror the sibling ``rewind`` boundary, which
    # already type-checks and length-caps its own ``content``.
    raw_content = body.get("content")
    if raw_content is not None and not isinstance(raw_content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"}, status=400
        )
    content = (raw_content or "").strip()
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "content_required"}, status=400
        )
    if len(content) > _MAX_EDIT_CONTENT_CHARS:
        return web.json_response(
            {
                "error": f"content too long (max {_MAX_EDIT_CONTENT_CHARS} chars)",
                "code": "content_too_long",
            },
            status=400,
        )

    async with slot._lock:
        # Reading the body above was an await, and ``linked_session_key`` can
        # be rebound on an already-live slot by a cron or workflow injection, so
        # a slow caller can be authorized against its own session and land on
        # somebody else's conversation. Re-authorize before checking admission
        # on whichever conversation the slot now routes to.
        stale = _reauthorize_after_await(state, slot, name, request_app, "chat.slot_edit_resend")
        if stale is not None:
            return stale

        busy = _destructive_history_busy(slot)
        if busy is not None:
            return busy

        # The session whose native resume identity the discard below clears.
        # Resolved here because the two guards that follow are about THAT
        # session, not about this slot's own task.
        session_key = effective_session_key(slot)

        # The slot admission reservation is not the whole "is this session
        # busy" question, and ``discard_conversation`` is a full teardown. Both guards below are the
        # ones the sibling teardown route (``reset-conversation``) already
        # applies before the SAME call, in the same order and with the same
        # codes -- reused rather than respelled, so the two cannot drift.
        if slot._in_stage_execution:
            # Defensive fallback for stage execution that has not yet
            # published its task or boundary reservation. An ordinary pending
            # stage was already refused by the admission guard above.
            return web.json_response(
                {"error": "slot is orchestrating", "code": "slot_orchestrating", "slot": name},
                status=409,
            )
        # The discard also releases the shared sub-agent runtime the parent's
        # children run on. ``slot.running`` can be False while they keep going
        # (the parent turn ends first), so nothing above catches it and a child's
        # work would be destroyed by an edit it has no part in.
        attached = await _subagents_attached_response(
            state, slot, session_key, "chat.slot_edit_resend"
        )
        if attached is not None:
            return attached

        msgs = slot.messages

        if ts:
            index = next(
                (i for i, m in enumerate(msgs) if m.get("ts") == ts and m.get("role") == "user"),
                -1,
            )
            if not isinstance(index, int) or index < 0:
                return web.json_response(
                    {"error": "user message not found for ts", "code": "user_message_not_found"},
                    status=400,
                )
        elif isinstance(index, int) and 0 <= index < len(msgs):
            if msgs[index].get("role") != "user":
                return web.json_response(
                    {"error": "index is not a user message", "code": "index_not_user_message"},
                    status=400,
                )
        else:
            return web.json_response(
                {"error": "index or ts required", "code": "index_or_ts_required"}, status=400
            )

        # Capture routing state BEFORE any live mutation, exactly like rewind:
        # the truncation is a real conversation boundary, so the native ACP
        # conversation must be discarded and the truncated history durably saved
        # before the live slot adopts the edit or any replacement turn is
        # dispatched. ``expected_history_key`` is the transcript this edit was
        # authorized against (``session_key``, the session whose native resume
        # identity is cleared, was resolved with the busy guards above).
        expected_history_key = slot_history_key(slot)

        # Prepare the truncated+edited window on a COPY. The dirty-slot flush
        # can run while either durable boundary below is pending, so exposing a
        # truncated live window here could make a rejected edit permanent.
        # This edited value is BOTH the persisted user row (``append`` below)
        # and the turn's input (``_run_chat`` runs the same ``_bc``), so the
        # session's own human's edit is delivered AS TYPED -- the rule an
        # ordinary send follows -- and redacting it would strip a link the human
        # kept in the message from the model. An app-driven edit-resend
        # (``request_app`` set) is not the reader's own words and stays
        # display-redacted, matching ``queue_entry_is_user_origin``'s boundary
        # and the ``_directive_user_origin=not bool(request_app)`` stamp below.
        # ``not request_app`` is the whole owner test here, not a narrowing of
        # that discriminator: this HTTP endpoint carries only the dashboard
        # composer or an app, so a channel or producer ``kind`` stamp cannot
        # reach it -- the sole question left is whether an app drives the edit.
        _bc = queued_text_for_display(content, user_origin=not bool(request_app))
        prospective_slot = copy.copy(slot)
        prospective_slot.messages = list(slot.messages[:index])
        # ``copy.copy`` is SHALLOW, so every mutable attribute still IS the live
        # slot's object. Reassigning ``messages`` alone is not enough, because
        # ``_ChatSlot.append`` below writes through four more of them: it
        # appends to ``_pending``, ``set()``s ``event``, filters
        # ``_question_pending``, and fires ``_on_question_retired``. On an
        # un-severed copy that publishes the edited row into the LIVE stream
        # reader's queue and announces the live question cards as retired
        # BEFORE any of the five rejection points below (failed discard, busy
        # session, failed flush, refused save, rebound slot) can refuse the edit
        # -- so a refused edit leaves a phantom row and card-less "needs input"
        # behind.
        # Sever all five; the commit re-adopts them, and only then. ``_queue``
        # is copied rather than emptied because, unlike rewind, edit-resend does
        # not discard queued sends -- the copy exists so no mutation on this
        # scratch slot can ever reach the live queue.
        prospective_slot._queue = list(slot._queue)
        prospective_slot._pending = list(slot._pending)
        prospective_slot._question_pending = dict(slot._question_pending)
        prospective_slot._on_question_retired = None
        prospective_slot.event = asyncio.Event()
        if prospective_slot._pending:
            prospective_slot.event.set()
        prospective_slot._dirty = True
        prospective_slot._resumed_count = 0
        prospective_slot.append("user", _bc, "msg msg-u")
        msgs_snapshot = list(prospective_slot.messages)
        # Which question cards the prospective append retired. Announced at
        # commit time instead, through the LIVE callback the copy was denied.
        retired_question_ids = [
            question_id
            for question_id in slot._question_pending
            if question_id not in prospective_slot._question_pending
        ]

        # The backing queue as it stood BEFORE the reservation below. An entry
        # arriving after it was diverted there by the reservation and has no
        # drain trigger of its own, so an abort must hand it off.
        pre_await_queue_ids = {item["id"] for item in slot._queue}

        # The live window and pending queue as they stand BEFORE the awaits. A
        # row absent from these arrived DURING them and belongs to the NEW
        # timeline, so the commit must carry it rather than replace it away --
        # the same rule rewind states for an entry queued during its own
        # boundary. Identity is the row OBJECT: a positional cut breaks the
        # moment ``append``'s own trim drops leading rows, and a restore-path row
        # carries no ``meta.mid`` to key on. An id cannot be recycled before the
        # commit because every pre-await row stays referenced throughout -- the
        # prefix by ``prospective_slot.messages``, the discarded suffix by the
        # live ``slot.messages``.
        # RETAINED lists, not just the id sets. An ``id()`` is an integer that
        # says nothing about the object's lifetime, and nothing else here keeps
        # the pre-await rows alive: the window trims at the cap
        # (``_MAX_SLOT_MESSAGES``) and a low-index edit pins nothing ahead of
        # ``index`` (at index 0 the prospective copy is empty), so a leading row
        # can be freed while the awaits below run and CPython can hand its id to a
        # newly appended arrival. The commit would then read that arrival as "not
        # new" and drop it -- the exact loss this snapshot exists to prevent.
        # Holding the rows keeps every id unique to the object that minted it.
        # ``meta.mid`` is not an alternative identity: ``append`` skips it for
        # restored rows (``mint_mid=False``) and for the wire-only roles.
        pre_await_rows = list(slot.messages)
        pre_await_row_ids = {id(row) for row in pre_await_rows}
        pre_await_pending = list(slot._pending)
        pre_await_pending_ids = {id(row) for row in pre_await_pending}

        # Reserve the slot BEFORE the awaits below. ``slot.turn_running`` derives
        # from ``slot.task``, and the send path is not serialized on
        # ``slot._lock``: without a live task, a send arriving while any of the
        # three durable boundaries below is pending observes an IDLE slot,
        # appends its row to ``slot.messages`` and dispatches a competing turn
        # -- which the commit below would then erase. Publishing the dispatch
        # task here (no await between the idle check above and this assignment)
        # makes such a send take the queue path instead; the entry is not in
        # ``pre_await_queue_ids``, so on abort the task hands it to the
        # canonical successor dispatch and it is never stranded. The turn itself
        # runs only once ``dispatch_commit`` is set, so the reservation never
        # dispatches an edit the boundaries refused.
        dispatch_ready = asyncio.Event()
        dispatch_commit = False

        async def _edit_resend_dispatch() -> None:
            await dispatch_ready.wait()
            if dispatch_commit:
                await _run_chat(
                    state,
                    slot,
                    _bc,
                    _directive_user_origin=not bool(request_app),
                    # See ``api_chat``: an observed app must be NAMED, because the
                    # actor resolver's fallback is ``user``. ``""`` is the
                    # parameter's own default and reads as "not named".
                    _turn_actor="app" if request_app else "",
                )
                return
            # Edit rejected. A send diverted to the queue by this reservation
            # has no drain trigger of its own (no turn ran), so hand it to the
            # canonical successor dispatch, which re-validates holds before
            # starting anything. Entries queued BEFORE the reservation keep
            # waiting for their own trigger.
            if any(entry["id"] not in pre_await_queue_ids for entry in slot._queue):
                if await _start_next_queued_turn(state, slot):
                    return
            state.push_slots_update()

        task = asyncio.create_task(_edit_resend_dispatch())
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error(
                    "edit-resend _run_chat failed for %s", slot.key, exc_info=t.exception()
                )

        task.add_done_callback(_on_done)

        try:
            # Durably clear the native conversation BEFORE the history rewrite,
            # mirroring rewind. A failure here leaves the original branch intact
            # and dispatches no replacement turn.
            def _sel_native_destroyed(reason: str, *, native_cleared: str = "1") -> None:
                """Record a destroyed native context that never reached a commit.

                ``discard_conversation`` plus ``aflush`` are irreversible: past
                that point the provider-side conversation is gone whether or not
                this request goes on to succeed. SEL already carries this
                endpoint's denials and its successful commits, so without this
                record the ONE outcome that destroyed context WITHOUT committing
                anything is the only one missing from the audit trail -- and it
                is the only one that cannot be reconstructed from the others,
                because in the trail it is indistinguishable from a denial that
                touched nothing.

                ``native_cleared`` is a THREE-valued field, not a flag, because
                the teardown has three outcomes and only two of them are facts:
                it happened, it did not, or the check that would have told us
                raised. ``"unknown"`` is what the third writes. A boolean here
                forced the one case the audit exists for to be spelled as one of
                the other two, and an audit that goes quiet on the outcome it
                could not determine is worse than none: its silence reads as
                nothing to report.
                """
                sel().log_api_access(
                    caller=request_app or "dashboard",
                    operation="chat.edit_resend",
                    outcome="error",
                    source="dashboard",
                    resources=f"slot={slot.key},native_cleared={native_cleared}",
                    error=reason,
                )

            if state.sessions is not None:
                # Shielded and drained for the same reason the history save below
                # is: ``discard_conversation`` pops the session and calls
                # ``clear_sid`` BEFORE its own remaining awaits
                # (``to_thread(unlink)``, ``provider.shutdown()``,
                # ``release_subagent_runtime``), so the destruction is already
                # true while those run. A client disconnect landing there would
                # otherwise propagate past every handler below with the context
                # gone and nothing recorded. The shield does not make the teardown
                # slower -- it was always going to run to completion -- it only
                # keeps this handler alive long enough to learn the outcome.
                discard_task = asyncio.ensure_future(
                    # ``skip_if_busy``: an inbound channel turn holds the session
                    # semaphore while ``slot.turn_running`` reads False, so the idle
                    # check above cannot see it -- an unconditional discard would
                    # tear down its provider mid-reply.
                    state.sessions.discard_conversation(session_key, skip_if_busy=True)
                )
                try:
                    discarded = await asyncio.shield(discard_task)
                except asyncio.CancelledError:
                    # Drain to learn whether the teardown actually happened. The
                    # outcome is THREE-valued and the code says so: it destroyed
                    # (record it), it refused because the session was busy and
                    # destroyed nothing (record nothing), or we could not find out
                    # (record the unknown). Collapsing the third into
                    # ``destroyed = False`` asserted a fact this branch does not
                    # have, and suppressed the audit event on the one path most
                    # likely to need it.
                    #
                    # Bounded re-shield rather than a single ``await``, exactly as
                    # the history-rewrite drain below does: this await is itself a
                    # cancellation point, so one further cancel -- a gateway
                    # shutdown reaching a handler already unwinding from a client
                    # disconnect -- would abandon the drain and lose the record.
                    for _ in range(_SAVE_DRAIN_ATTEMPTS):
                        if discard_task.done():
                            break
                        try:
                            await asyncio.shield(discard_task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if discard_task.done() and not discard_task.cancelled():
                        discard_exc = discard_task.exception()
                        if discard_exc is not None:
                            # Recorded, not swallowed: the trail gets the
                            # undetermined outcome and the log gets the cause. The
                            # catch above stays broad on purpose --
                            # ``provider.shutdown()`` is provider transport and its
                            # failure modes are not enumerable from here, and
                            # letting an arbitrary error out of a
                            # ``CancelledError`` handler would REPLACE the client's
                            # cancellation with an unrelated exception. It narrows
                            # where it matters: ``Exception`` leaves
                            # ``CancelledError``, ``KeyboardInterrupt`` and
                            # ``SystemExit`` free to surface.
                            logger.warning(
                                "edit-resend: the discard for %s raised while draining "
                                "a cancellation, so whether the native context was "
                                "torn down is undetermined",
                                session_key,
                                exc_info=discard_exc,
                            )
                            _sel_native_destroyed(
                                "discard_cancelled_outcome_unknown", native_cleared="unknown"
                            )
                        elif discard_task.result():
                            _sel_native_destroyed("discard_cancelled")
                    else:
                        logger.warning(
                            "edit-resend: the discard for %s did not settle within %d "
                            "cancellation(s), so whether the native context was torn "
                            "down is undetermined",
                            session_key,
                            _SAVE_DRAIN_ATTEMPTS,
                        )
                        _sel_native_destroyed(
                            "discard_cancelled_outcome_unknown", native_cleared="unknown"
                        )
                    raise
                except Exception:
                    logger.warning(
                        "edit-resend: failed to discard ACP conversation for %s",
                        session_key,
                        exc_info=True,
                    )
                    # The raise can land on either side of this teardown's own
                    # destruction point: ``discard_conversation`` pops the session
                    # and calls ``clear_sid`` before its remaining awaits, so a
                    # failure inside it proves nothing either way. Record the
                    # undetermined outcome rather than nothing -- a silent exit
                    # here is indistinguishable in the trail from a refusal that
                    # touched no state, which is exactly the confusion the audit
                    # exists to remove.
                    _sel_native_destroyed(
                        "discard_failed_outcome_unknown", native_cleared="unknown"
                    )
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "could not prepare edited conversation; retry the edit",
                            "code": "edit_resend_prepare_failed",
                        },
                        status=503,
                    )
                if not discarded:
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "the session is busy with another reply; retry the edit",
                            "code": "edit_resend_session_busy",
                        },
                        status=409,
                    )
                try:
                    # Force the durability point endpoint-side: the sid clear
                    # lands in the session map's debounced writer, and a gateway
                    # exit before that write would resurrect the discarded
                    # conversation on restart.
                    await state.sessions.aflush()
                except asyncio.CancelledError:
                    # ``discarded`` is already True here, so the native context
                    # IS gone -- and ``CancelledError`` derives from
                    # BaseException, so the handler below cannot absorb it. A
                    # client disconnect landing on this await would otherwise
                    # leave the destruction with no record at all, which is the
                    # one outcome this audit exists for. Record, then let the
                    # cancellation propagate untouched.
                    _sel_native_destroyed("sid_flush_cancelled")
                    raise
                except Exception:
                    logger.warning(
                        "edit-resend: failed to flush the cleared resume sid for %s",
                        session_key,
                        exc_info=True,
                    )
                    # The in-memory discard already happened, so the native
                    # context is gone even though its sid clear is not durable.
                    _sel_native_destroyed("sid_flush_failed")
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "could not prepare edited conversation; retry the edit",
                            "code": "edit_resend_prepare_failed",
                        },
                        status=503,
                    )

            def _commit_target_intact() -> bool:
                """Whether the slot is still the one this edit was authorized against.

                Three axes can move across the boundary awaits, and each makes a
                commit land somewhere it was never authorized to. ONE predicate
                so the success path and the cancellation path cannot check
                different subsets -- the asymmetry that let a rebind through
                before.
                """
                if slot_history_key(slot) != expected_history_key:
                    # A cron or workflow injection re-linked the slot, hydrating
                    # it with ANOTHER conversation's state. The prospective copy
                    # froze the old routing, so the save-side
                    # ``expected_history_key`` guard cannot see the LIVE slot
                    # move -- this loop-side check is the one that can.
                    logger.warning(
                        "edit-resend: slot %s was rebound to another transcript during "
                        "persistence; refusing the commit",
                        slot.key,
                    )
                    return False
                if state._slots.get(name) is not slot:
                    # A close-and-recreate under the same name is a DIFFERENT
                    # conversation that a name-based check would wave through.
                    # Requires the same OBJECT, the same discipline
                    # ``_reauthorize_after_await`` applies to the body-read await.
                    logger.warning(
                        "edit-resend: slot %s was replaced during persistence; "
                        "refusing the commit",
                        slot.key,
                    )
                    return False
                if slot.task is not task:
                    # The reservation was displaced (a close cancelled it, or
                    # another dispatcher took the slot). Committing would leave
                    # this handler's turn running ALONGSIDE whatever now owns
                    # ``slot.task`` -- two concurrent turns writing one window.
                    logger.warning(
                        "edit-resend: the dispatch reservation for %s was displaced during "
                        "persistence; refusing the commit",
                        slot.key,
                    )
                    return False
                return True

            def _commit_live_state() -> None:
                """Adopt the prepared state on the live slot (synchronous).

                Shared by the normal success path and the cancellation path
                below: once the destructive rewrite has landed on disk, this is
                the only thing that keeps the live slot matching it. No await
                inside, so it is atomic on the event loop. Dispatching is the
                caller's separate ``dispatch_commit`` step, so a cancellation
                landing between the two cannot commit without dispatching.
                """
                # Carry the rows that landed on the LIVE slot while the
                # boundaries were pending. A workflow or cron completion appends
                # WITHOUT taking ``slot._lock`` (``workflow_inject`` calls
                # ``append_and_surface`` straight on the event loop), so a
                # wholesale replace drops the injected row -- and the rewrite
                # above cannot put it back, because a rewrite deliberately skips
                # the cross-process-append scan (``collect_foreign=not rewrite``
                # in ``chat_persistence``). Keeping it in the window is what
                # makes the next ORDINARY flush re-persist it. Appending them
                # AFTER the prospective window is the correct order and not just
                # a convenient one: ``monotonic_transcript_ts`` only ever moves a
                # row forward, so an arrived row can never be stamped EARLIER
                # than the edited one. It can be stamped IDENTICALLY -- on a
                # coarse clock (Windows ticks in ~15.6 ms steps) both appends read
                # the same instant -- and list order is what separates that tie,
                # which is why the merge order matters rather than a re-sort.
                arrived_rows = [row for row in slot.messages if id(row) not in pre_await_row_ids]
                arrived_pending = [
                    row for row in slot._pending if id(row) not in pre_await_pending_ids
                ]
                # Both containers are edited IN PLACE rather than replaced, and
                # the reason is one rule: a copy frozen before the awaits cannot
                # carry any write that landed during them. Answering a card pops
                # the id from the LIVE dict (``clear_pending``), and ``drain()``
                # does ``slot._pending.clear()`` on the LIVE list, so assigning
                # either frozen copy back resurrects an answered card or requeues
                # an already-delivered row. Deleting exactly what the edit
                # retired, and dropping only rows the edit's own snapshot has
                # since lost, leaves every concurrent write standing --
                # ``mark_pending`` is the only writer that ADDS a card, so a card
                # that arrived mid-boundary survives too. Announce only the ids
                # actually removed.
                announce_retired = [
                    question_id
                    for question_id in retired_question_ids
                    if slot._question_pending.pop(question_id, None) is not None
                ]
                slot.messages = prospective_slot.messages + arrived_rows
                delivered_pending_ids = pre_await_pending_ids - {id(row) for row in slot._pending}
                slot._pending[:] = [
                    row
                    for row in prospective_slot._pending + arrived_pending
                    if id(row) not in delivered_pending_ids
                ]
                slot.invalidate_source_links()
                slot._dirty = True
                slot._resumed_count = 0
                # ``total_messages`` is a LIFETIME counter that survives
                # trimming, and the prospective ``append`` bumped only the COPY's
                # int -- so without this the edited row is invisible to every
                # reader of it: ``_get_active_workspace`` picks the max-counter
                # slot to resolve which workspace's lessons to load, and the
                # Slack mirror compares the counter against its own start value
                # to decide whether anything happened. Incremented by ONE here
                # rather than adopted from the copy, whose value predates the
                # arrived rows above (which bumped the live counter themselves).
                # Truncation deliberately does not decrement it.
                slot.total_messages += 1
                # Deliberately NOT copied from ``prospective_slot``: the
                # persistence witnesses (``_pending_rewrite``, ``_disk_*``,
                # ``_frozen_prefix_cache``). The save above ran on the LIVE slot
                # and stamped them with the post-rewrite truth; the prospective
                # copies are the PRE-save values, and restoring those would
                # re-arm a destructive rewrite on the next flush and move the
                # monotone ``_disk_tail_ts`` floor backwards.
                if slot._pending:
                    slot.event.set()
                else:
                    slot.event.clear()
                if announce_retired and callable(slot._on_question_retired):
                    try:
                        slot._on_question_retired(slot.key, announce_retired)  # type: ignore[operator]
                    except Exception:
                        logger.debug(
                            "edit-resend: question-retirement announcement failed for slot %s",
                            slot.key,
                            exc_info=True,
                        )

                sel().log_api_access(
                    caller=request_app or "dashboard",
                    operation="chat.edit_resend",
                    outcome="allowed",
                    source="dashboard",
                    resources=slot.key,
                )

            # Persist the truncated+edited history via the explicit-snapshot
            # rewrite path. Nothing was mutated on the live slot yet, so a
            # failure (exception OR a save refused by its own guards) means
            # nothing persisted: no live mutation and no dispatch.
            #
            # The worker thread cannot be interrupted: once the rewrite starts it
            # WILL finish, whether or not this handler is still alive. A client
            # disconnect cancels the handler task, and a bare await here would
            # then abandon a completed destructive rewrite -- persisted history
            # truncated, the live slot still holding the full original window,
            # and the next periodic dirty-slot flush re-serializing that stale
            # window back over the truncated file. That is exactly the "live
            # window desynchronized from disk" failure this change set out to
            # close, so shield the save; on cancellation, wait for the worker's
            # real outcome and, if the rewrite landed on the transcript we
            # authorized, commit the live slot to match disk and let the reserved
            # dispatch run the edited prompt before propagating the cancellation.
            # Through ``save_slot_off_loop``, not a bare ``to_thread``, and the
            # difference is load-bearing rather than stylistic. Because the live
            # slot keeps the FULL window until the commit, the periodic
            # dirty-slot flush can snapshot that stale window, block behind this
            # rewrite on the per-session history lock, and then write the
            # snapshot back on top -- restoring every message the rewrite just
            # discarded. The helper bumps ``slot._metadata_persist_inflight``
            # around the write, which is exactly the flag ``flush_slot_now``
            # already honours to keep "this unpinned periodic writer" off a slot
            # with a guarded write pending, and it decrements in a ``finally``.
            # Shielding the wrapper (rather than the inner future) is what keeps
            # that exclusion held for the whole write: a cancellation reaching
            # the shield leaves the coroutine running, so its ``finally`` does
            # not release the flag early. ``best_effort=False`` so a failure
            # propagates to the 503 below instead of being swallowed and
            # re-armed as a dirty retry.
            #
            # Both axes are pinned INTO the write, because the commit boundary is
            # the only place either can be decided. ``expected_history_key``
            # catches a RENAMED replacement; it cannot see a same-name
            # close-and-recreate, which resumes the same transcript and so keeps
            # the key identical. ``expected_slot_name`` carries this slot's map
            # key in, where ``state._slots[name]`` is re-read inside the
            # transcript lock with no await before the write: a map holding a
            # different slot object refuses the save, nothing written. The
            # loop-side identity check above cannot stand in for it -- the
            # recreate can land during the executor wait, after that check and
            # before the write -- and the loop-side check is still needed for the
            # reservation axis (``slot.task``), which the persistence layer
            # cannot see. A refusal returns ``False`` and reaches the 503 below
            # with the live slot untouched.
            save_task = asyncio.ensure_future(
                save_slot_off_loop(
                    state,
                    slot,
                    msgs_snapshot,
                    best_effort=False,
                    expected_history_key=expected_history_key,
                    expected_slot_name=name,
                )
            )
            try:
                saved = await asyncio.shield(save_task)
            except asyncio.CancelledError:
                # Drain the worker's real outcome, shielded and RETRIED. A
                # SECOND cancellation -- a gateway shutdown arriving while this
                # handler is already unwinding from a client disconnect -- lands
                # on whatever await sits here, and ``CancelledError`` is a
                # BaseException, so an ``except Exception`` cannot absorb it. A
                # bare ``await save_task`` therefore abandons a rewrite the
                # worker thread finishes anyway: disk truncated, live slot still
                # holding the discarded suffix, the next dirty-slot flush pushing
                # that stale window back over the truncated file -- exactly the
                # desync this boundary exists to close. Shielding each attempt
                # keeps the worker's future alive across those cancellations, and
                # the outcome is read off the settled task rather than awaited,
                # so it cannot be lost to a cancel landing between the two.
                # Bounded: a cancel storm must not spin here.
                landed = False
                for _ in range(_SAVE_DRAIN_ATTEMPTS):
                    if save_task.done():
                        break
                    try:
                        await asyncio.shield(save_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if save_task.done() and not save_task.cancelled():
                    save_exc = save_task.exception()
                    landed = save_exc is None and bool(save_task.result())
                elif not save_task.done():
                    logger.warning(
                        "edit-resend: the history rewrite for %s did not settle within "
                        "%d cancellation(s); leaving the live slot untouched",
                        slot.key,
                        _SAVE_DRAIN_ATTEMPTS,
                    )
                if landed and _commit_target_intact():
                    _commit_live_state()
                    dispatch_commit = True
                    logger.info(
                        "edit-resend: request cancelled after the rewrite landed for %s; "
                        "committed live state and dispatching the edited prompt",
                        slot.key,
                    )
                else:
                    # The native context is already gone and nothing was
                    # committed against it: either the rewrite did not land, or
                    # it landed on a slot that moved. This is the same
                    # destroyed-without-a-commit outcome as the 503 paths below,
                    # and it is the one exit where the client is not even told --
                    # the cancellation propagates instead of a response, so the
                    # SEL record is the ONLY place it can be attributed from.
                    _sel_native_destroyed("request_cancelled")
                raise
            except Exception:
                logger.warning("edit-resend: failed to persist", exc_info=True)
                _sel_native_destroyed("history_save_exception")
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not save edited conversation; retry the edit",
                        "code": "edit_resend_save_failed",
                    },
                    status=503,
                )
            if not saved:
                # The save's own guards refused the write (the session was
                # permanently deleted, or the slot was rebound to another
                # transcript, while the write awaited its lock). Nothing was
                # persisted, so dispatching a turn now would run from state that
                # exists only in memory.
                logger.warning(
                    "edit-resend: history save refused for %s (concurrent delete or rebind)",
                    slot.key,
                )
                _sel_native_destroyed("history_save_refused")
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not save edited conversation; retry the edit",
                        "code": "edit_resend_save_failed",
                    },
                    status=503,
                )

            # Both irreversible boundaries succeeded. Before adopting the
            # prepared state, confirm the slot is still the one this edit was
            # authorized against, on all three axes that can move across the
            # awaits above. No await between these checks and the mutations
            # below, so the decision cannot go stale.
            if not _commit_target_intact():
                _sel_native_destroyed("commit_target_moved")
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "the conversation changed while saving; retry the edit",
                        "code": "edit_resend_slot_rebound",
                    },
                    status=503,
                )

            # Both boundaries committed. Adopt the prepared state on the LIVE
            # slot, then release the reserved dispatch. No await between the save
            # above and these mutations, so they are atomic on the event loop.
            #
            # And NOTHING may await between here and ``dispatch_ready.set()`` in
            # the ``finally`` below, because ``dispatch_commit`` is already True by
            # then: an await there lets a cron completion rebind the slot, and the
            # released dispatch would run this handler's prompt against ANOTHER
            # conversation. That rules out a second guarded save for a row the
            # commit carried -- the commit sets ``_dirty``, so the merged window
            # reaches disk on the next periodic flush like any ordinary append,
            # and re-checking the commit target after such an await could not
            # rescue it either: refusing once the live slot has adopted the
            # truncated window would leave a truncation with no turn.
            _commit_live_state()
            dispatch_commit = True
        finally:
            # Wake the reserved dispatch task on every exit: it runs the
            # replacement turn on commit and the queue handoff on abort.
            dispatch_ready.set()

    state.push_slots_update()
    return web.json_response({"ok": True})
