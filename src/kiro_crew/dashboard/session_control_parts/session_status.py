"""What a caller reads back: the status roster of the sessions it created, a target's
transcript tail and its cached summary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.session_tree_projection import projection
from kiro_crew.dashboard.chat_delivery import sanitize_outbound
from kiro_crew.dashboard.chat_persistence import _TRANSIENT_ROLES as _PERSISTENCE_TRANSIENT_ROLES
from kiro_crew.dashboard.chat_persistence import _recent_session_slot_name
from kiro_crew.dashboard.session_control_parts.caller_access import (
    SessionControlError,
    _audit,
    _caller_is_ownership_fenced,
)
from kiro_crew.dashboard.session_control_parts.model_switch import _pending_pick_view
from kiro_crew.dashboard.session_control_parts.session_messaging import broadcast_audience
from kiro_crew.dashboard.session_control_parts.target_authorization import (
    _broadcast_resolution_slots,
    _deny_factory,
    authorize_target,
    refuse_caller_identity,
    refuse_caller_surface,
)
from kiro_crew.metrics.sessions import END_REASON_REMOVED
from kiro_crew.metrics.turns import turn_outcome
from kiro_crew.security import redact, redact_and_truncate
from kiro_crew.session_summary import derive_state
from kiro_crew.validation import MAX_SESSION_STATUS_ROWS, MAX_SESSION_STATUS_TITLE_CHARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


# Reads are cheap but not free — each one walks the target's in-memory window.
MAX_READ_MESSAGES = 100


DEFAULT_READ_MESSAGES = 20


# Per-message content cap for reads, so pulling a transcript tail cannot return
# a multi-megabyte tool payload verbatim.
MAX_READ_CONTENT_CHARS = 4000


# Cap on the tool title a read or status row names as waiting for approval.
MAX_PENDING_APPROVAL_TOOL_CHARS = 200


# Bounds on a ``read_summary`` response. The stored summary caps intent and note
# COUNTS at 50 but neither per-intent list lengths nor string lengths, so the
# response is cut here, once, and says how much it left out. Intents arrive
# most-recently-touched first, so the intent cut drops the oldest.
MAX_SUMMARY_INTENTS = 10


MAX_SUMMARY_ITEMS = 5


MAX_SUMMARY_NOTES = 10


MAX_SUMMARY_CHARS = 500


# Roles a read must not count, taken from the persistence layer's own list rather
# than restated here: those are exactly the rows rehydration DROPS, so any cursor
# that counted them would name a different position after a restart than before
# it. ``chunk`` runs are deleted when a segment flushes and ``done`` markers never
# persist at all, so counting either inflates ``total``, the list shrinks back
# under it, and the next ``since=next_since`` read skips the finished reply for good.
TRANSIENT_ROLES = _PERSISTENCE_TRANSIENT_ROLES


def _created_tree_roster(caller_key: str) -> "tuple[list[str], str]":
    """Slots the crew log says hang under *caller_key*, and how good that read was.

    The DURABLE half of :func:`created_session_status`. ``state._slots`` knows only
    what is open right now, so a worker that was closed, or whose session was lost
    to a crash, disappears from it entirely — and "I dispatched eight and can see
    six" is exactly the question this verb exists to answer. The crew log's session
    tree is written when a session is opened and survives both, so it is what
    supplies the roster while the live slots supply the status.

    The second value is the read's own quality, and it is returned rather than
    folded into the list because the three answers are not interchangeable:

    * ``readable`` — the fold saw every unit the store holds.
    * ``incomplete`` — a unit's bytes could not be read, or the population was
      capped, so rows may be MISSING. The fold is still served: this reader
      DISPLAYS lineage rather than deciding on it, which is the case
      :class:`~kiro_crew.crew_log.session_tree.TreeReading` documents as free to
      ignore the flag — but a caller counting its workers must be told the count
      is a floor.
    * ``unreadable`` — the crew log is off, or the projection is not seeded for the
      store configured now. There is no roster at all and the answer is live-only.

    Reads the in-memory fold and does NO I/O, so it is safe on the event loop. An
    unseeded projection answers ``unreadable`` rather than seeding itself here,
    for the reason :func:`_slot_tree_parent` gives: a seed is a disk read.

    The parent edge it follows is the CURRENT one, so an adopted session is listed
    under whoever holds it now rather than under whoever opened it. That is the
    right answer for this verb — it reports the sessions the caller is answerable
    for — and it cannot widen the caller's reach, because an adoption is itself
    gated by :func:`authorize_target`.
    """
    try:
        if not crew_log_emit.enabled():
            return [], "unreadable"
        proj = projection()
        if not proj.seeded_for_current_store:
            return [], "unreadable"
        reading = proj.reading()
    except Exception:
        logger.debug("session tree roster could not be read", exc_info=True)
        return [], "unreadable"
    children: list[str] = []
    overflow = False
    for slot, node in reading.nodes.items():
        if getattr(node, "parent_slot", None) != caller_key or slot == caller_key:
            continue
        if len(children) < MAX_SESSION_STATUS_ROWS:
            children.append(slot)
        else:
            overflow = True
    children.sort()
    return children, ("incomplete" if reading.incomplete or overflow else "readable")


def _gone_slot_lifecycles(slots: "Sequence[str]") -> dict[str, dict[str, Any]]:
    """Fold each gone slot's lifecycle so a `gone` row can say CLOSED versus LOST.

    The crew log already records a ``session/closed`` edge and the status fold
    already projects it as ``lifecycle``/``closed_at`` (see
    :func:`~kiro_crew.crew_log.projection._status_render`). This reads that
    projection per slot and nothing more -- no new terminal vocabulary, no new
    verb -- so a worker whose tab was closed after it finished is told apart from
    one lost with its process.

    The read is slot-keyed (:func:`~kiro_crew.crew_log.projection.read_slot_projection`),
    so it folds over EVERY unit the slot ran under, newest last, and reports the
    lifecycle of the slot's current session. A slot that was reopened and later
    closed again reads ``closed``; the ``session/opened`` of the reopen resets the
    close the earlier life recorded, which the status fold already handles.

    Returns a map ``{slot: {"status", "closed_at"?}}``. A slot is ``closed`` (and
    carries its ``closed_at``) only when its fold's newest life ended by the
    DELIBERATE tab close -- ``close_reason`` is ``removed`` and no turn is still
    open (see :func:`_lifecycle_is_closed`). Everything else is ``lost``: an
    ``open``/``unknown`` lifecycle, a read that raised, OR a ``closed`` lifecycle
    whose edge came from a process recycle (``reset``: idle expiry, RSS recycle,
    model/provider switch, the watchdogs) or a ``destroy`` (including mid-turn) --
    teardowns the tab and conversation outlive. "Lost" is the fail-safe reading: it
    is the state a patrol re-dispatches, and reporting a worker lost when it may
    have finished is cheaper than reading a recycled or mid-turn worker as
    ``closed`` and telling a conductor to stop looking for it.

    Touches disk (one slot-keyed fold per slot), so it is called off the event loop.
    """
    from kiro_crew.crew_log.projection import read_slot_projection

    out: dict[str, dict[str, Any]] = {}
    for slot in slots:
        try:
            value = read_slot_projection(slot, "status").value
        except Exception:
            # A read fault is not evidence the worker closed cleanly, so it reads as
            # LOST -- the fail-safe answer, the one a patrol acts on by re-dispatching.
            logger.debug("gone row lifecycle could not be folded for %s", slot, exc_info=True)
            out[slot] = {"status": "lost"}
            continue
        if _lifecycle_is_closed(value):
            out[slot] = {"status": "closed", "closed_at": value.get("closed_at")}
        else:
            out[slot] = {"status": "lost"}
    return out


#: Close reasons that mean "the worker FINISHED and its tab was closed" -- the one
#: direction a gone row may call ``closed``. Only the deliberate tab ✕ / ``session_close``
#: teardown (``remove`` -> ``END_REASON_REMOVED``) qualifies. Every OTHER close edge is a
#: process recycle or a mid-turn teardown that the tab and conversation survive --
#: ``reset`` (idle expiry, RSS recycle, model/provider switch, the watchdogs),
#: ``destroyed``/``destroyed_sid_retained`` (``destroy``, including mid-turn) -- and must
#: read ``lost``, the fail-safe state a patrol re-dispatches. Reading one of those as
#: ``closed`` is the fail-OPEN harm this split exists to remove: it would tell a conductor
#: a lost worker finished, so it stops looking for it.
_FINISHED_CLOSE_REASONS = frozenset({END_REASON_REMOVED})


def _lifecycle_is_closed(value: "Mapping[str, Any]") -> bool:
    """Whether a folded slot status means the worker finished and closed its tab.

    ``True`` only when the fold's newest life is closed by the DELIBERATE end
    (``close_reason`` in :data:`_FINISHED_CLOSE_REASONS`), no turn is still open,
    AND the newest completed turn ended cleanly. A ``closed`` lifecycle alone is
    not enough: the ``session/closed`` edge is also written on every process
    recycle (``reset``) and on ``destroy`` (including mid-turn), teardowns the tab
    and conversation outlive.

    The ``last_stop_reason`` guard is the third fail-safe, and it is the one a
    deliberate MID-TURN close needs: closing a tab with a turn in flight cancels
    ``slot.task``, and the cancelled turn's ``finally`` writes a real
    ``turn/completed`` (``stop_reason`` ``cancelled``/``failed``/``error:*``) that
    CLEARS ``turn_open`` -- so the ``turn_open`` guard above does not catch it, and
    a worker killed mid-turn would read ``closed`` ("finished, nothing to
    re-dispatch") when its work did not finish. A newest turn that did not end
    cleanly (:func:`turn_outcome` other than ``"ok"``) therefore reads ``lost``.
    A clean ``end_turn`` and the ``None`` of a tab closed before any turn ran both
    classify ``"ok"`` -- a session that genuinely finished (or never started work)
    stays ``closed``.

    A close whose reason is a recycle, a close with a turn still open, a newest
    turn that did not end cleanly, or any non-closed lifecycle all read as
    not-closed -> the caller reports ``lost``, the fail-safe direction.
    """
    if value.get("lifecycle") != "closed":
        return False
    if value.get("turn_open"):
        # A session cut off with a turn in flight did not finish, however it was
        # torn down -- the projection leaves the open turn set on a close for
        # exactly this reader to see (crew_log/projection_side_panel.py::_status_render).
        return False
    if turn_outcome(value.get("last_stop_reason")) != "ok":
        # The newest completed turn did not end cleanly. A deliberate mid-turn
        # close cancels the running turn, whose ``finally`` records a
        # ``turn/completed`` with an abnormal ``stop_reason`` and clears
        # ``turn_open`` -- so this is the only guard that still sees the work was
        # cut off. ``None``/``end_turn`` are both ``"ok"``, so a clean finish and a
        # never-worked tab close stay ``closed``.
        return False
    return value.get("close_reason") in _FINISHED_CLOSE_REASONS


def _bounded_status_title(value: object) -> str:
    """A display-safe title bounded before it is retained in a status row."""
    return sanitize_outbound(str(value or ""))[:MAX_SESSION_STATUS_TITLE_CHARS]


def process_state(state: "DashboardState", slot: Any) -> str:
    """``"live"`` while *slot*'s session holds a running process, else ``"released"``.

    A released session keeps its conversation: the idle sweep stops the process
    and the next turn resumes it through ``session/load`` (session.md, "Idle
    cleanup"). Read from the session manager's live table
    (``SessionManager.has_session``, keyed by the slot's effective session key).
    """
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    sessions = getattr(state, "sessions", None)
    try:
        live = sessions is not None and sessions.has_session(effective_session_key(slot))
    except Exception:  # noqa: BLE001 - an unanswerable probe reads as released
        logger.debug("process probe failed for %s", getattr(slot, "key", "?"), exc_info=True)
        live = False
    return "live" if live else "released"


def _pending_approval_fields(state: "DashboardState", slot: Any) -> dict[str, Any]:
    """``{"pending_approval": True, "pending_approval_tool": ...}`` while *slot* waits.

    A turn parked on a tool approval keeps ``running`` true, so without this a
    creator polling the session sees ``working`` for the whole approval window.
    The answer is the slot projection's own ``pending_approval`` and
    ``pending_approval_info`` -- the fields the dashboard card reads -- so the
    two cannot disagree. ``to_dict`` walks the transcript, so it runs only once
    one of the two registries it consults has an open entry; an ordinary turn
    pays one dict scan and the response shape is unchanged.
    """
    futures = getattr(slot, "_approval_futures", None) or {}
    pending_for = getattr(state, "pending_coordinator_approvals", None)
    if not any(not fut.done() for fut in futures.values()) and not (
        callable(pending_for) and pending_for(slot.key)
    ):
        return {}
    projected = slot.to_dict()
    if not projected.get("pending_approval"):
        return {}
    fields: dict[str, Any] = {"pending_approval": True}
    tool = str((projected.get("pending_approval_info") or {}).get("tool") or "")
    if tool:
        fields["pending_approval_tool"] = redact_and_truncate(
            sanitize_outbound(tool), MAX_PENDING_APPROVAL_TOOL_CHARS
        )
    return fields


def _created_history_roster(
    state: "DashboardState", caller_key: str, caller_workspace: str
) -> tuple[dict[str, dict[str, Any]], str, int]:
    """Persisted birth metadata for sessions created by *caller_key*.

    This is the transcript-meta half of :func:`created_session_status`. It is a
    weaker source than the fenced crew log: ``created_by`` is already trusted by
    the member ownership boundary, but it is not gateway-authored lineage. Rows
    retain that distinction through their ``source`` value.

    The scan is read-only. ``list_sessions`` supplies the catalog and
    ``get_metadata_status`` supplies both the creator edge and a readability
    verdict for each candidate. A bad metadata row or a matching population past
    :data:`MAX_SESSION_STATUS_ROWS` makes the result ``incomplete`` rather than
    disappearing behind a claim of completeness. Only the bounded title needed
    by the response is retained; the full editable metadata dict is not.
    """
    log = state.conversation_log
    if log is None:
        return {}, "unreadable", 0
    try:
        sessions = log.list_sessions()
    except Exception:
        logger.debug("session history roster could not be listed", exc_info=True)
        return {}, "unreadable", 0

    rows: dict[str, dict[str, Any]] = {}
    incomplete = False
    omitted = 0
    for session in sessions:
        history_key = str(session.get("key", ""))
        slot_key = _recent_session_slot_name(history_key)
        if slot_key is None or slot_key == caller_key:
            continue
        try:
            meta, readable = log.get_metadata_status(history_key)
        except Exception:
            logger.debug("session history metadata could not be read", exc_info=True)
            incomplete = True
            continue
        if not readable:
            incomplete = True
            continue
        if str(meta.get("created_by", "")) != caller_key:
            continue
        if str(meta.get("workspace", "default")) != caller_workspace:
            continue
        if len(rows) >= MAX_SESSION_STATUS_ROWS:
            # Keep scanning only to report the exact overflow count. No metadata
            # or title beyond the cap is retained in this request.
            omitted += 1
            incomplete = True
            continue
        rows[slot_key] = {
            "title": _bounded_status_title(meta.get("title") or slot_key),
        }
    return rows, ("incomplete" if incomplete else "readable"), omitted


async def created_session_status(
    state: "DashboardState",
    *,
    caller_session_key: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Every session this caller stood up, and what each one is doing now.

    The roster and the status come from THREE sources because none can answer
    every half. The crew log's session tree is durable and gateway-attested, while
    persisted history metadata records a birth before the first turn can write a
    tree edge. Live slots know exactly what is running but forget a session the
    moment it is gone. The union covers all three windows, and each row says which
    source placed it.

    ``status`` per row, and the five are distinct answers a patrol acts on
    differently:

    * ``working`` — a turn is in flight. Wait.
    * ``queued`` — idle, but messages are waiting to run. Also wait, but nothing is
      happening yet, so a steer would land on nothing.
    * ``idle`` — open and doing nothing. This is the one that needs a decision.
    * ``closed`` — the crew log names it, the dashboard does not hold it, and the
      log records a ``session/closed`` edge: a worker that FINISHED and closed its
      tab. Carries ``closed_at``. Ignore it; there is nothing here to message, and
      :func:`authorize_target` would answer ``target_not_found``.
    * ``lost`` — the crew log names it, the dashboard does not hold it, and no
      qualifying clean deliberate close was recorded: either no ``session/closed``
      edge at all, or the newest one is a ``reset``/``destroy`` teardown or a close
      whose last turn did not end cleanly — none of which means "finished, nothing
      to re-dispatch". Lost with the process that ran it (or its opener was retained
      out of the log). Re-dispatch it. ``closed`` and ``lost`` are the two fates the
      old single ``gone`` status could not tell apart, and they call for opposite
      actions.
    * ``unknown`` — history records the creator but neither a live slot nor an
      attested tree edge exists. The row proves the session was created without
      claiming whether it finished or was lost.

    READ-ONLY. The history catalog and metadata reads touch disk; callers keep the
    operation informational and expose their quality separately from the crew-log
    tree quality.

    The union is bounded at :data:`MAX_SESSION_STATUS_ROWS`, and that cut gets its
    OWN field: ``roster_omitted`` counts the rows dropped, zero when the whole
    union was retained. It is deliberately not expressed by degrading ``history``,
    which describes the transcript scan alone — a union cut is not a read fault in
    any source, and spelling it that way blames a read that completed while
    ``history_omitted`` still reports nothing cut.

    The ownership fence applies to the rows, not just to the verb: a fenced caller
    (a crew member, a cron, an agent-created session) sees the live sessions it
    created and nothing else, so this verb cannot become a way to enumerate the
    user's own sessions by their titles. A ``closed`` or ``lost`` row carries only
    a slot key the caller already knew, and no title, so it is listed either way.
    """
    deny = _deny_factory(caller_session_key=caller_session_key, operation="status", target="")
    caller_key = refuse_caller_identity(state, caller_session_key=caller_session_key, deny=deny)
    caller_slot = refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    ownership_fenced = (
        _caller_is_ownership_fenced(state, caller_key) if caller_fenced is None else caller_fenced
    )
    tree_children, tree_state = _created_tree_roster(caller_key)

    # The first suspension in this verb, deliberately AFTER the complete
    # synchronous caller gate and fence resolution above. The route prewarms the
    # config cache immediately before entering this coroutine, so moving an await
    # into that window would let an intervening config edit put blocking config IO
    # back on the loop. The scan itself globs, stats and reads transcript metadata,
    # so run the complete operation in one worker-thread hop.
    caller_workspace = str(getattr(caller_slot, "workspace", "default"))
    history_children, history_state, history_omitted = await asyncio.to_thread(
        _created_history_roster, state, caller_key, caller_workspace
    )
    # The gate above RAN before the suspension, but its verdict does not survive
    # one: a channel mirror can be bound onto an already-open dashboard session
    # while this scan is on a worker thread -- the channel picker
    # (`messaging/session_resume.py`) and the Slack link route
    # (`slack/interactions.py`) both do it with no idle-slot requirement -- and
    # the rows below carry other sessions' TITLES, the names of the user's
    # private work. Published past a gate that passed, they reach that channel's
    # audience. So re-call the same gate on the same terms and refuse through the
    # same audited path, the way the queue drain re-checks mirror identity
    # between admission and delivery (`_probe_channel_mirror`) rather than
    # trusting its admission.
    #
    # Slot IDENTITY is deliberately NOT compared: `get_or_create_slot` returns the
    # registered slot for an existing key and the single `put_slot` publish runs
    # only where the key was absent, so no live key is rebound to a new object --
    # a check for it could never fire. WORKSPACE is compared, because that one is
    # reassigned on a live slot (`chat_handlers` commits one, `channel_slots`
    # adopts one from metadata) and it is the boundary every row below is filtered
    # on: a workspace that moved mid-scan would filter rows against a boundary the
    # history scan did not use.
    refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    if str(getattr(caller_slot, "workspace", "default")) != caller_workspace:
        raise deny(
            "the calling session moved workspace while its roster was being read; " "call again",
            "caller_changed_mid_read",
        )

    # Live slot state stays on the event loop and is read only after the history
    # worker returns. Reading it inside the worker would let the result go stale
    # before these rows are built. This read only names the caller's own creations
    # (`_created_by`, not workspace), so it is not the workspace-boundary read --
    # that is `resolvable_keys`, built below AFTER the last suspension in this verb.
    live_children = broadcast_audience(state, caller_key)

    rows: list[dict[str, Any]] = []
    roster = set(tree_children) | set(history_children) | set(live_children)
    # The UNION cut is its own fact, with its own field and its own count. It must
    # not be signalled by degrading ``history_state``, which describes the
    # transcript scan alone: a union cut is not a read fault in any source, so that
    # spelling says two false things at once -- it blames a transcript read that
    # completed, and it leaves ``history_omitted`` reporting nothing cut while rows
    # are dropped. Neither is actionable, because re-reading history cannot recover
    # a row the union bound cut.
    #
    # Zero when nothing was cut, so a reader distinguishes "no overflow" from a
    # cut whose size it must know to judge the roster it was handed.
    roster_omitted = max(0, len(roster) - MAX_SESSION_STATUS_ROWS)
    retained = sorted(roster)[:MAX_SESSION_STATUS_ROWS]
    # A `gone` row -- the tree names it and the dashboard does not hold it -- splits
    # into `closed` and `lost`, and the two call for OPPOSITE actions: ignore a
    # worker that finished and closed its tab, re-dispatch one lost with its
    # process. The split is read from the ``session/closed`` edge the status fold
    # already projects, so it is a disk fold per gone slot and runs OFF the loop,
    # the same worker-thread discipline the history scan above keeps. Scoped to the
    # retained, tree-attested, now-absent slots so no fold is paid for a row that
    # will not carry the distinction.
    gone_slots = [key for key in retained if key in tree_children and state.get_slot(key) is None]
    gone_lifecycles = (
        await asyncio.to_thread(_gone_slot_lifecycles, gone_slots) if gone_slots else {}
    )
    # The gone fold above is the LAST suspension in this verb, and the live-row
    # containment must be computed after it, not before. A live worker can be moved
    # across the workspace boundary mid-fold -- `api_chat_slot_workspace` reassigns
    # `_slot.workspace` on a running slot with no idle requirement -- so a
    # `resolvable_keys` built before this await would admit a worker on its
    # pre-move workspace and then carry its `display_title` out of the caller's
    # workspace in the row loop below. So re-call the caller gate on the same terms
    # and REBUILD containment here, after the final await, exactly as the block
    # after the history scan does: the boundary every row is filtered on is read
    # from the live slots as they stand once nothing else can suspend.
    refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    if str(getattr(caller_slot, "workspace", "default")) != caller_workspace:
        raise deny(
            "the calling session moved workspace while its roster was being read; " "call again",
            "caller_changed_mid_read",
        )
    # The SAME containment set `session_broadcast` resolves names against, resolved
    # here once and applied to every live row below. Built AFTER the gone fold's
    # await (and its re-check), so a worker moved across the boundary during that
    # fold is already excluded -- its `workspace` is re-read here as it now stands,
    # and the fail-safe follows: a row the moved worker would carry is dropped by
    # the `resolvable_keys` membership test below rather than leaked.
    resolvable_keys = {
        slot.key
        for slot in _broadcast_resolution_slots(
            state,
            caller_key=caller_key,
            caller_slot=caller_slot,
            ownership_fenced=ownership_fenced,
        )
    }
    for key in retained:
        slot = state.get_slot(key)
        from_tree = key in tree_children
        from_history = key in history_children
        sources = [
            source
            for source, present in (
                ("crew_log", from_tree),
                ("history", from_history),
                ("live", slot is not None),
            )
            if present
        ]
        if slot is None:
            if from_tree:
                # The attested tree still owns this status. History may corroborate
                # the birth, but it does not turn editable metadata into lineage.
                #
                # CLOSED vs LOST, read from the ``session/closed`` edge the status
                # fold projects: a worker that closed its tab after finishing is
                # ``closed`` and carries its ``closed_at``; one gone without a
                # recorded close -- lost with its process, or whose opener was
                # retained out of the log -- is ``lost``. Defaults to ``lost`` when
                # the fold was not made (a slot absent from the map), the fail-safe
                # reading a patrol re-dispatches rather than drops.
                lifecycle = gone_lifecycles.get(key, {"status": "lost"})
                gone_row: dict[str, Any] = {
                    "target": key,
                    "status": lifecycle["status"],
                    "source": "+".join(sources),
                }
                if lifecycle["status"] == "closed":
                    gone_row["closed_at"] = lifecycle.get("closed_at")
                rows.append(gone_row)
                continue
            if not from_history:
                # Neither tree nor history, and no live slot. A row reaches here
                # only because it was in ``retained`` -- read from the creator's
                # live children BEFORE the gone-fold await -- and its slot has since
                # gone: a worker the caller created that never took a first turn (so
                # no tree edge) and left no history row (the history scan filters on
                # workspace) can have its tab closed WHILE the fold runs on its
                # worker thread, landing here with ``slot is None`` and both durable
                # sources absent. Reading ``history_children[key]`` would raise
                # ``KeyError`` and fail the whole roster. Drop the row: the worker is
                # gone with no durable record to report, which is the fail-safe
                # reading -- the same "nothing to say about a slot that vanished with
                # no attested trace" the ``lost`` default elsewhere takes.
                continue
            meta = history_children[key]
            rows.append(
                {
                    "target": key,
                    "title": meta["title"],
                    "status": "unknown",
                    "source": "history",
                }
            )
            continue
        if slot.key not in resolvable_keys:
            # ONE predicate for both verbs. These rows carry `display_title`, which
            # for a channel-linked or mirrored session is derived from a conversation
            # other people are in, and a creator whose worker is linked LATER would
            # otherwise be handed that title on its next roster read. The row is
            # dropped rather than reported title-less for the reason the workspace
            # clause inside the predicate gives: `session_send` refuses this target
            # too (`linked_session_target`, `mirrored_target`, `ephemeral_target`,
            # `app_scoped_target`, `not_creator`), so a row here would be a listing
            # of work the caller cannot message.
            #
            # Membership in `_broadcast_resolution_slots` rather than a second copy
            # of its clauses, because one containment enforced in two places is one
            # containment that can differ between them: a clause restated here is a
            # clause that has to be restated again on every change to the predicate.
            continue
        queue_depth = len(slot._queue)
        running = bool(slot.running)
        rows.append(
            {
                "target": key,
                "title": _bounded_status_title(slot.display_title),
                "status": "working" if running else ("queued" if queue_depth else "idle"),
                "running": running,
                "queue_depth": queue_depth,
                "process": process_state(state, slot),
                **_pending_approval_fields(state, slot),
                "source": "+".join(sources),
            }
        )

    _audit(
        caller_session_key=caller_session_key,
        operation="status",
        slot_key=caller_key,
        outcome="allowed",
        detail={
            "rows": len(rows),
            "tree": tree_state,
            "history": history_state,
            "history_omitted": history_omitted,
            "roster_omitted": roster_omitted,
        },
    )
    return {
        "ok": True,
        "caller": caller_key,
        # How much of the durable roster this answer rests on. A caller that reads
        # only `sessions` and gets a short list cannot tell "you created three" from
        # "I could read three", and for a patrol deciding whether a worker was lost
        # those are opposite conclusions.
        "tree": tree_state,
        # History metadata is a separate, weaker roster source. Its quality must
        # not be folded into `tree`, whose value describes only the crew-log fold.
        "history": history_state,
        # Exact count of matching transcript rows not retained due to the bound.
        # Kept separate from ``history`` so callers can distinguish a read fault
        # from deliberate overflow while old clients still see ``incomplete``.
        "history_omitted": history_omitted,
        # Exact count of rows the UNION of the three sources lost to
        # ``MAX_SESSION_STATUS_ROWS``. Separate from ``history_omitted`` because it
        # is a different cut with a different remedy: this one is not a read fault
        # in any source, and no source's quality field describes it. Zero when the
        # whole union was retained.
        "roster_omitted": roster_omitted,
        "sessions": rows,
    }


def read_messages(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    limit: int = DEFAULT_READ_MESSAGES,
    since: int | None = None,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Read *target*'s transcript tail plus enough state to poll it.

    ``next_since`` is the cursor to poll with; passing it back as ``since`` on the
    next call returns only what arrived in between, which is the whole
    wait → read poll loop. ``running`` says whether the target is still
    working, so a caller knows the difference between "nothing new yet" and
    "finished and idle".
    """
    if limit < 1 or limit > MAX_READ_MESSAGES:
        raise SessionControlError(
            f"limit must be between 1 and {MAX_READ_MESSAGES}", code="invalid_limit"
        )
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="read",
        precomputed_ownership_fenced=caller_fenced,
    )
    # The reader's own audience was recorded by the caller-side gate inside
    # ``authorize_target`` (:func:`record_audience_admission`), so a mirror gained
    # or retargeted between this read and the reply it feeds withholds the
    # reader's cross-surface publication. No per-verb stamp here: the gate is the
    # one point every verb passes, which is what keeps a read path from missing it.

    # Indexes are ABSOLUTE positions in the session, not offsets into the live
    # window. A slot keeps only the most recent ``_MAX_SLOT_MESSAGES`` in memory
    # and credits each trimmed row to a frozen-prefix counter, so window length
    # stops growing once trimming starts. A cursor derived from that length
    # would freeze at the cap and never see another reply; adding the
    # frozen-prefix count makes it monotonic for the session's whole life.
    raw_window = list(slot.messages)
    # The DURABLE frozen-prefix counter, never ``_disk_older_count``: that one
    # counts every trimmed row, transient ones included, while the positions
    # below are built over the durable rows the filter keeps. Basing on the
    # all-rows counter shifted every position as soon as a transient row was
    # trimmed, and a ``since`` read then served a durable message the caller
    # already had. ``_disk_older_durable_count`` counts exactly the rows the
    # ``TRANSIENT_ROLES`` filter below would have kept, so the two spaces agree
    # for the session's whole life. Defensive ``getattr`` matches how the
    # existing code reads ``_disk_older_count``: a slot restored by an older
    # build simply has no trimmed prefix yet.
    base = int(getattr(slot, "_disk_older_durable_count", 0) or 0)
    # Stop the cursor before the streaming tail (see ``TRANSIENT_ROLES``): those
    # rows are deleted when the segment flushes, so a cursor past them would sit
    # beyond the list that replaces them and never return the finished reply.
    messages = [m for m in raw_window if m.get("role") not in TRANSIENT_ROLES]
    durable_end = len(messages)
    total = base + durable_end
    if since is not None:
        if since < 0:
            raise SessionControlError("since must be >= 0", code="invalid_since")
        if since < base:
            # The cursor points into the trimmed prefix: those rows exist only
            # on disk now, and this read serves the in-memory window. Starting
            # at ``base`` instead would silently skip every row in
            # ``[since, base)`` — a poller that lagged a whole window behind
            # would lose messages with nothing in the response saying so. The
            # refusal is loud and the tail-read fallback recovers, exactly like
            # the past-the-end case below.
            raise SessionControlError(
                "this session is long enough that the messages at your cursor "
                "have been trimmed from memory — read without `since` to get "
                "the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        # A cursor PAST the end is the remaining inexact case, and it is not the
        # same as a stale one: rewind and regenerate shrink a transcript, so
        # `total` can move backwards under a caller that is still holding the old
        # position. Clamping it to `total` would start the read at the end and
        # silently skip every replacement row written below the old cursor, with
        # nothing in the response saying so. So this refuses loudly rather than
        # answer approximately. Reads without `since` are unaffected.
        if since > total:
            raise SessionControlError(
                "this session is shorter than your cursor — it was rewound or "
                "regenerated, so earlier positions no longer line up — read "
                "without `since` to get the latest messages",
                status=409,
                code="cursor_unavailable",
            )
        start = since
        # Positions are absolute; the window slice below is offset-relative, so
        # subtract the durable prefix that is not held in memory.
        offset = start - base
    else:
        # A tail read never refuses (only a `since` below the trimmed prefix or
        # past the end is), and the two spaces come apart here: slice the
        # in-memory window by OFFSET, but report the index in ABSOLUTE terms so
        # the number still means "position in the session". Conflating them
        # returned an empty window, because `total` counts the frozen prefix
        # the list does not hold.
        offset = max(0, durable_end - limit)
        start = base + offset
    window = messages[offset:][:limit]

    out: list[dict[str, Any]] = []
    for offset, msg in enumerate(window):
        content = str(msg.get("content", "") or "")
        # ``redact_and_truncate`` scans the COMPLETE text before slicing. Cutting
        # first would split a credential straddling the boundary into a prefix
        # that does not match the scanner, so the fragment would ship.
        emitted = redact_and_truncate(content, MAX_READ_CONTENT_CHARS)
        row: dict[str, Any] = {
            "index": start + offset,
            "role": str(msg.get("role", "") or ""),
            "content": emitted,
            "ts": str(msg.get("ts", "") or ""),
        }
        if len(content) > MAX_READ_CONTENT_CHARS:
            row["truncated"] = True
        out.append(row)

    _audit(
        caller_session_key=caller_session_key,
        operation="read",
        slot_key=slot.key,
        outcome="allowed",
        detail={"returned": len(out)},
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": sanitize_outbound(slot.display_title),
        # Busy means "more output is coming", which is exactly what a poller needs
        # to decide whether to wait.
        "running": bool(slot.running),
        # True when the target is mid-reply: rows exist that the cursor
        # deliberately does not cover yet, so "nothing new" here does not mean
        # "nothing happening".
        **({"streaming": True} if durable_end < len(raw_window) else {}),
        "queue_depth": len(slot._queue),
        # A turn parked on a tool approval is still ``running``; this says it is
        # waiting on a person rather than working.
        **_pending_approval_fields(state, slot),
        # The model the target's turns use, and a session_set_model pick still
        # waiting for its next turn, so a caller can see whether its pick took.
        # Redacted: the owner's picker stores whatever string it is given.
        # The served model when the backend has reported one (it reflects an
        # inherited default or an active fallback), otherwise the pin.
        "model": redact(slot.served_model or slot.model or ""),
        **_pending_pick_view(slot._pending_model_pick),
        "reasoning_effort": slot.reasoning_effort or "",
        "total": total,
        # The cursor to poll with next. This is NOT `total`: when more than
        # `limit` rows are new, the window stops short of the end, and a caller
        # that polled `since=total` would jump the gap and never see the rows in
        # between. `next_since` is the absolute position just past the last row
        # actually returned, so consecutive polls cover every row exactly once.
        # `total` stays in the response as the backlog depth — the difference
        # from `next_since` is how far behind the caller still is.
        #
        # Returned on trimmed sessions too: positions are based on the
        # durable-only prefix counter, so they stay exact after rows age into
        # the frozen prefix. The refusals above cover the cases that genuinely
        # cannot be exact (a cursor under the trimmed prefix, or past the end of
        # a rewound transcript).
        "next_since": start + len(out),
        "messages": out,
    }


def _summary_text(value: object) -> str:
    """One summary string, redacted and then cut to ``MAX_SUMMARY_CHARS``."""
    text = value if isinstance(value, str) else ""
    # Redact the whole string first (the helper's own order), then cut, so the
    # marker reflects what the redacted text lost.
    redacted = redact_and_truncate(text, len(text) * 4 + 256)
    if len(redacted) <= MAX_SUMMARY_CHARS:
        return redacted
    return redacted[:MAX_SUMMARY_CHARS] + " …[truncated]"


def _bounded_summary(payload: dict) -> dict[str, Any]:
    """The fields a patrol reads, bounded, with a count of what each cut dropped.

    Redaction runs on every emitted string through ``redact_and_truncate``, the
    sink ``read_messages`` uses: the sidecar was redacted when written, and
    re-redacting on the way out covers a scrubber rule added since.
    """
    raw_intents = [i for i in (payload.get("intents") or []) if isinstance(i, dict)]
    intents: list[dict[str, Any]] = []
    for intent in raw_intents[:MAX_SUMMARY_INTENTS]:
        progress = [p for p in (intent.get("progress") or []) if isinstance(p, str)]
        steps = [st for st in (intent.get("next_steps") or []) if isinstance(st, dict)]
        intents.append(
            {
                "title": _summary_text(intent.get("title")),
                # The panel's single word, not the raw progress axis: a
                # ``completed`` intent that was never verified reads
                # ``needs-you``, which is the case a patrol most needs to see.
                "state": _summary_text(
                    intent.get("state")
                    or derive_state(str(intent.get("status") or ""), intent.get("verified"))
                ),
                # The latest progress is what a patrol needs; the next steps in
                # the order the summarizer ranked them.
                "progress": [_summary_text(p) for p in progress[-MAX_SUMMARY_ITEMS:]],
                "progress_omitted": max(0, len(progress) - MAX_SUMMARY_ITEMS),
                "next_steps": [_summary_text(st.get("what")) for st in steps[:MAX_SUMMARY_ITEMS]],
                "next_steps_omitted": max(0, len(steps) - MAX_SUMMARY_ITEMS),
            }
        )
    notes = [n for n in (payload.get("constraints") or []) if isinstance(n, str)]
    return {
        "intents": intents,
        "intents_omitted": max(0, len(raw_intents) - MAX_SUMMARY_INTENTS),
        "constraints": [_summary_text(n) for n in notes[:MAX_SUMMARY_NOTES]],
        "constraints_omitted": max(0, len(notes) - MAX_SUMMARY_NOTES),
    }


async def read_summary(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Return *target*'s cached intent summary, the one the side panel shows.

    Authorized by the gate :func:`read_messages` uses (``authorize_target``):
    the summary is derived from the transcript, so a caller that may not read
    the transcript may not read its digest either. ``operation`` only labels
    the audit line, so a refusal reads as ``session_control.summary``.

    A cache read only. It never generates, so it never spends a model call:
    summaries are written at turn end by the background pass, or on the panel's
    explicit POST, and neither is reachable from here. Gated on
    ``session_summary.enabled`` like the panel GET, so switching the feature
    off stops serving sidecars written while it was on.

    The ``authorize_target`` gate runs before the first suspension, so the
    handler's ``prewarm_enabled_check`` stays valid across it.
    """
    # Late import: chat_summary pulls in the chat package, which imports this
    # module at load time.
    from kiro_crew.dashboard.chat_summary import read_cached_intent_summary

    def _authorize(*, recheck: bool) -> "_ChatSlot":
        return authorize_target(
            state,
            caller_session_key=caller_session_key,
            target=target,
            operation="summary",
            skip_enabled_check=recheck,
            precomputed_ownership_fenced=caller_fenced,
        )

    slot = _authorize(recheck=False)
    running = bool(slot.running)

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    enabled = bool(cfg.session_summary.enabled)
    payload: dict | None = None
    stale = False
    log = state.conversation_log
    if enabled and log is not None:
        payload, stale = await read_cached_intent_summary(log, slot)
    # Both reads above suspend, the sidecar read for as long as the transcript
    # lock's acquire ceiling. The caller or the target can gain a channel link
    # or a mirror in that window, so the gate runs again, synchronously, before
    # anything is returned, and the answer must still be the same slot.
    if _authorize(recheck=True) is not slot:
        raise SessionControlError(
            "the target session was replaced while its summary was read; try again",
            status=409,
            code="target_replaced",
        )
    bounded = _bounded_summary(payload or {})

    _audit(
        caller_session_key=caller_session_key,
        operation="summary",
        slot_key=slot.key,
        outcome="allowed",
        detail={
            "enabled": enabled,
            "present": payload is not None,
            "intents": len(bounded["intents"]),
        },
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": sanitize_outbound(slot.display_title),
        "running": running,
        "enabled": enabled,
        "stale": stale,
        "generated_at": (payload or {}).get("generated_at"),
        **bounded,
    }
