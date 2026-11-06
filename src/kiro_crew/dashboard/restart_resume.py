"""Pressing Resume on the person's behalf, within a budget (rfc-restart-resume).

Two passes, one mechanism. The **boot pass** (:func:`resume_after_restart`) walks
the slots the startup restores rebuilt and, for every chat whose metadata line
said the previous process died with a turn admitted and whose tail reads as
interrupted (:func:`restart_candidate`), queues the same continuation the Resume
button queues. The **error pass** (:func:`schedule_after_error`) runs
when a local turn ends in an ``error`` row whose structural kind is on the
retryable list, and arms one backed-off retry for that chat. Both dispatch
through :func:`chat_handlers.continue_slot_turn`, so every state the button
refuses is refused here too, the row lands as the same ``inject`` the button
lands, and the SEL ``dashboard_continue`` line is written with a ``via`` that
names this module.

Nothing here widens authority: the continuation is queued with no human-origin
flag and no app or cron identity, and the resumed turn runs under whatever
approval mode the slot holds when it is dispatched. Trust is in memory by
construction and this module does not change that; what it adds is the notice
(:func:`approval_mode_dropped_notice`) that tells the person the mode changed.

Everything here is inert while ``dashboard.resume_interrupted_turns`` is off (the
default): each pass reads the switch before it arms or dispatches, the error pass
reads it again after its backoff, and the notice pass returns at once. Only the
record of what the person last saw (``approval_mode_seen``, which every save
writes) is kept either way, so the first boot after the switch is turned on
compares the mode honestly. Whether the notice should also reach a gateway that
never turns the switch on is a decision rfc-restart-resume §9 leaves to the
maintainers; this module does not make it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import random
import time
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

from kiro_crew.config.sections import (
    MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE,
    RESUME_RETRY_LIMIT_MIN,
    RESUME_RETRY_WINDOW_MIN,
)
from kiro_crew.dashboard.approval_mode_record import live_approval_mode
from kiro_crew.dashboard.auto_resume_stamps import (  # noqa: F401 -- the validator lives beside the codec; re-exported for this module's readers
    MAX_STAMPS,
    read_persisted_stamps,
)
from kiro_crew.dashboard.chat_utils import (
    _AUTO_RESUME_ERROR_MSG,
    _AUTO_RESUME_RESTART_MSG,
    RETRY_INVITED_KIND,
    SESSION_START_FAILED_KIND,
    STOPPED_META,
    TRANSIENT_NOTICE_GIVE_UP,
    TRANSIENT_NOTICE_META_KEY,
    TRANSIENT_RETRY_KIND,
    TURN_FAILED_META,
    effective_session_key,
    run_to_completion,
    slot_history_key,
)
from kiro_crew.dashboard.relay_archive import is_relay_archive
from kiro_crew.dashboard.slot_persistence.turn_marker import _RESTART_INTERRUPTION_KIND
from kiro_crew.dashboard.slot_persistence.write_guards import register_guarded_history_write
from kiro_crew.dashboard.state import (
    _is_turn_inject,
    is_stop_event_row,
    is_turn_interrupted,
    load_window_off_loop,
)
from kiro_crew.dashboard.system_notices import (
    APPROVAL_MODE_DROPPED_KIND,
    AUTO_RESUME_PAUSED_KIND,
    is_system_notice,
)
from kiro_crew.members import DM_SLOT_MODE

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: ``via`` values on the ``dashboard_continue`` SEL line, so the audit trail tells
#: an automatic resume from a press (no ``via``) and from the ``session_retry`` /
#: ``session_continue`` tools (``session_control``).
VIA_RESTART = "restart_resume"
VIA_ERROR = "error_resume"

#: The cause words the inject row's ``meta.autoResume.cause`` carries; the
#: ``RecoveryCard`` reads the prefix line for the title and this for the detail.
CAUSE_RESTART = "restart"
CAUSE_ERROR = "error"

#: Terminal error rows a retry can plausibly cure, by structural kind: a failed
#: session start, and the runner's own "please retry" rows (``RETRY_INVITED_KIND``,
#: stamped by the writers that ask for a retry once their in-turn recovery gave
#: up). Fail closed: an ``error`` row with no kind, or with a kind not listed
#: here, is never retried, so a new terminal error is retried unattended only
#: once its writer tags it on purpose. The retry ladder's own give-up row
#: carries ``meta.notice`` rather than ``meta.kind`` and is handled beside this
#: set in :func:`error_retry_candidate`.
RETRYABLE_ERROR_KINDS: frozenset[str] = frozenset({SESSION_START_FAILED_KIND, RETRY_INVITED_KIND})

#: Error-pass backoff: the n-th retry inside the window waits
#: ``min(BASE * 2**(n-1), CAP)`` plus up to 25 % jitter. The base is sized for
#: "the in-turn ladder already spent minutes and gave up", not for a first 5xx.
BACKOFF_BASE_SECS = 30.0
BACKOFF_CAP_SECS = 600.0
BACKOFF_JITTER = 0.25

#: Boot pass pacing: a settle before the first dispatch so the restore's slot
#: broadcasts have landed, then a floor between chats so a dashboard holding
#: thirty interrupted chats does not start thirty agent processes in one instant
#: on a host that may have just crashed.
BOOT_SETTLE_SECS = 5.0
BOOT_STAGGER_SECS = 2.0

#: Seam for the error-pass backoff, so a test can hold the task inside its wait
#: without a wall-clock sleep (the same shape as the runner's ``_recovery_delay``).
_backoff_sleep = asyncio.sleep

_APPROVAL_MODE_LABELS = {
    "trust": "Trust (tools run without asking)",
    "trust_reads": "Trust reads (read-only commands run without asking)",
    "normal": "Ask every time",
}

#: The option's name in the composer's "Approval mode" control, for the remedy.
_APPROVAL_MODE_OPTIONS = {"trust": "Trust", "trust_reads": "Reads"}


# ── Settings ────────────────────────────────────────────────────────────────────


#: What the passes use when no config can be read: the switch off.
_SETTINGS_OFF: tuple[bool, int, int] = (False, 5, 3600)


def _int_or(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _settings(cfg: Any) -> tuple[bool, int, int]:
    """``(enabled, limit, window_secs)`` from a loaded config, defaults when absent."""
    dashboard = getattr(cfg, "dashboard", None)
    enabled = bool(getattr(dashboard, "resume_interrupted_turns", False))
    limit = _int_or(getattr(dashboard, "resume_retry_limit", None), 5)
    window_minutes = _int_or(getattr(dashboard, "resume_retry_window_minutes", None), 60)
    return (
        enabled,
        max(RESUME_RETRY_LIMIT_MIN, limit),
        max(RESUME_RETRY_WINDOW_MIN, window_minutes) * 60,
    )


def _load_settings() -> tuple[bool, int, int]:
    """The config in force now: the process watcher's snapshot, else a disk read.

    The disk read is why callers on the loop hand this to a thread.
    """
    # Imported here: the loader pulls the whole config package in behind it, and
    # this module is imported by the metadata codec on the slot-restore path.
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    snapshot = live.snapshot()
    if snapshot is not None:
        return _settings(snapshot)
    try:
        return _settings(KiroCrewConfig.load())
    except Exception:
        logger.debug(
            "restart_resume: config load failed; treating the switch as off", exc_info=True
        )
        return _SETTINGS_OFF


def _switch_on_now() -> bool | None:
    """The switch from the in-memory config snapshot, or None when none is armed yet."""
    from kiro_crew.config import live  # same reason as in ``_load_settings``

    snapshot = live.snapshot()
    return None if snapshot is None else _settings(snapshot)[0]


# ── The persisted budget ────────────────────────────────────────────────────────


def _prune(stamps: Iterable[float], window_secs: float, now: float) -> list[float]:
    floor = now - window_secs
    return [s for s in stamps if s >= floor]


def budget_used(slot: Any, *, window_secs: float, now: float | None = None) -> int:
    """How many automatic resumes *slot* spent inside the window, pruning stale stamps."""
    current = time.time() if now is None else now
    slot._auto_resume_stamps = _prune(slot._auto_resume_stamps, window_secs, current)
    return len(slot._auto_resume_stamps)


def reset_budget(slot: Any) -> None:
    """A person acted on the chat: the count starts over and any armed retry is dropped.

    Called from a person's send and a person's Resume press. Neither an agent's
    ``session_retry`` / ``session_continue`` nor an automatic resume calls it --
    the budget exists to bound exactly what no person asked for.
    """
    if slot._auto_resume_stamps:
        slot._auto_resume_stamps = []
        slot._dirty = True
        # The person's own turn writes the empty list with its turn marker, so a
        # gateway that dies before the periodic flush does not read the spent
        # budget back off the line and refuse what the person just reset.
        slot._auto_resume_reset_pending = True
    slot._restart_cut_turn = False
    cancel_armed_retry(slot)


def _unstamp(slot: Any, stamp: float) -> None:
    """Take back a stamp for a resume that did not run.

    The in-memory count is corrected at once. The line keeps the stamp until the
    next save carries the shorter list, so a gateway that dies in that gap counts
    a resume that never happened -- the fail-closed direction: at worst the
    automation does one resume less.
    """
    try:
        slot._auto_resume_stamps.remove(stamp)
    except ValueError:
        return
    slot._dirty = True


async def _commit_budget(state: Any, slot: Any) -> bool:
    """Write the chat's stamps to its metadata line and wait for the write.

    The dispatcher's persist-before-publish point: the stamp a resume spends is
    on disk BEFORE the resume is queued, so a gateway that dies on the resumed
    turn, or before the next periodic flush, counts it on the next boot and the
    budget can stop a resume-crash loop. ``False`` when nothing durable was
    written -- no log, a chat that is closing, a line that does not exist, a
    guard that refused, a failed write, or a write the log SKIPPED (a legacy
    row-first transcript keeps its first line, and ``update_metadata_if`` reports
    that no-op as success unless ``require_write`` is set) -- and the caller then
    refuses the resume: an uncounted automatic turn is the one thing the budget
    exists to prevent.
    Fenced to this slot incarnation AND to the list it carries, both decided
    under the line's lock, and registered with the close's wait the same way as
    the turn marker's own merge (``_begin_local_turn_marker``). The second fence
    is for a person's reset that lands while the write is in flight: a write that
    still carried the spent stamps would put them on the line after the reset
    emptied them, and the person's turn marker, seeing nothing pending, would
    never carry the empty list -- a restart would then read the spent budget
    back and pause what the person had just reset. So the write refuses when the
    list moved before its lock, and when it moved after, the reset stays pending
    for the marker to carry.
    """
    log = getattr(state, "conversation_log", None)
    if log is None or getattr(slot, "is_closing", False):
        return False
    history_key = slot_history_key(slot)
    slot_name = slot.key
    carried = list(slot._auto_resume_stamps)
    fields = {"auto_resume_stamps": carried}

    def _still_this_budget(_meta: dict) -> bool:
        return state._slots.get(slot_name) is slot and slot._auto_resume_stamps == carried

    merge = asyncio.get_running_loop().run_in_executor(
        None,
        functools.partial(
            log.update_metadata_if,
            history_key,
            fields,
            _still_this_budget,
            require_existing=True,
            require_write=True,
        ),
    )
    register_guarded_history_write(slot, merge)
    try:
        written = bool(await run_to_completion(merge))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "restart_resume: could not persist the budget for slot %s", slot.key, exc_info=True
        )
        return False
    if written and slot._auto_resume_stamps == carried:
        slot._auto_resume_reset_pending = False
    return written


def cancel_armed_retry(slot: Any) -> None:
    """Drop the one armed error-pass retry, if any, and supersede a dispatch in flight.

    Called by a person's send and Resume, a Stop and a close. Moving the epoch is
    what reaches a retry that already woke from its backoff: ``_dispatch`` re-reads
    it under the slot lock right before its queue insert and refuses when it moved.
    The restart marker is cleared too, so the boot pass's candidate re-check after
    its settle skips the chat: a Stop on an idle restored chat writes no Stop card
    (the no-op branch), and the marker flag is the only thing that re-check reads.
    The spent budget is left as it is -- a Stop is not a reset.
    """
    slot._restart_cut_turn = False
    slot._auto_resume_epoch = getattr(slot, "_auto_resume_epoch", 0) + 1
    task = getattr(slot, "_auto_resume_task", None)
    if task is not None:
        slot._auto_resume_task = None
        if not task.done():
            task.cancel()


# ── Classification ─────────────────────────────────────────────────────────────


def _newest_turn_rows(messages: list[dict]) -> list[dict]:
    """The rows after the newest turn-opening row, oldest first.

    A turn opens with a ``user`` or ``nudge`` row that has content, or an
    ``inject`` row whose meta says it dispatched a turn -- the same opener rule
    ``is_turn_interrupted`` applies, so a held note the runner flushes after an
    error (an untagged ``inject``) is looked through rather than read as a new
    turn that swallowed the error. Everything the newest opener is followed by
    -- replies, tool rows, status rows, an error, a Stop card -- is how that
    turn ended.
    """
    tail: list[dict] = []
    for row in reversed(messages):
        if _opens_a_turn(row):
            break
        tail.append(row)
    tail.reverse()
    return tail


def _opens_a_turn(row: dict) -> bool:
    role = row.get("role")
    if not row.get("content"):
        return False
    if role == "inject":
        return _is_turn_inject(row.get("meta"))
    return role in ("user", "nudge")


def _newest_error_row(turn_rows: list[dict]) -> dict | None:
    """The newest ``error`` row of the turn, or None when it ended another way.

    Looks through status rows (a compaction notice, our own pause notice) the way
    ``is_turn_interrupted`` does, and through the in-turn retry ladder's PENDING
    row (``TRANSIENT_RETRY_KIND``: a recovery was queued, the turn had not ended
    -- a restart during that wait leaves it as the newest error), and stops at a
    Stop card or a reply: an error row older than those is not how the turn
    ended.
    """
    for row in reversed(turn_rows):
        role = row.get("role")
        if role == "error":
            if _row_kind(row) == TRANSIENT_RETRY_KIND:
                continue
            return row
        if is_stop_event_row(row):
            return None
        if is_system_notice(role, row.get("meta")):
            continue
        if role in ("assistant", "subagent") and row.get("content"):
            return None
    return None


def _row_kind(row: dict) -> str | None:
    meta = row.get("meta")
    if isinstance(meta, dict):
        kind = meta.get("kind")
        if isinstance(kind, str):
            return kind
        notice = meta.get(TRANSIENT_NOTICE_META_KEY)
        if notice == TRANSIENT_NOTICE_GIVE_UP:
            return TRANSIENT_NOTICE_GIVE_UP
    return None


def _turn_failed_row(row: dict) -> bool:
    """A row written when the turn failed before it started (``TURN_FAILED_META``)."""
    meta = row.get("meta")
    return isinstance(meta, dict) and bool(meta.get(TURN_FAILED_META))


def _stopped_row(row: dict) -> bool:
    """A give-up row the ladder wrote because the person's Stop ended its backoff (``STOPPED_META``)."""
    meta = row.get("meta")
    return isinstance(meta, dict) and bool(meta.get(STOPPED_META))


def restart_candidate(slot: Any) -> bool:
    """Whether the boot pass may resume *slot*: a turn THIS restart cut off.

    Three things must hold, read after the window is loaded and ``turn_marker``
    has reconciled. The metadata line carried a turn-in-flight marker
    (``_restart_cut_turn``): the previous process admitted a turn it never
    finished, which the transcript alone cannot show -- and which an
    interruption row left by an EARLIER boot does not show either, so such a
    chat is not resumed on this one. The transcript reads as interrupted. And
    the newest turn ended in no error row at all (the marker's row is only
    appended when the tail did not already prove the interruption: an
    unanswered user row, a cut recovery or nudge turn), or in the restart row
    itself; a chat whose newest turn ended in some other error was failing
    before the restart and is not resumed on the restart's account.
    """
    if not getattr(slot, "_restart_cut_turn", False):
        return False
    messages = slot.messages
    if not is_turn_interrupted(messages):
        return False
    row = _newest_error_row(_newest_turn_rows(messages))
    return row is None or _row_kind(row) == _RESTART_INTERRUPTION_KIND


def error_retry_candidate(messages: list[dict]) -> bool:
    """Whether the transcript's newest turn ended in a retryable terminal error.

    The retry ladder's give-up row qualifies, so does a failed session start and
    a row whose writer invited the retry (:data:`RETRYABLE_ERROR_KINDS`). Never:
    a row with no structural kind or a kind off the list (an approval prompt
    nobody answered, "Session stuck", anything a writer has not tagged), the
    restart row (the boot pass owns that one), a turn that failed before it
    started (:data:`TURN_FAILED_META`), and any turn the person stopped -- a Stop
    card anywhere in the newest turn is their recorded intent that nothing
    continues on its own, and so is a give-up row the ladder marked
    :data:`STOPPED_META`: a Stop issued on a linked channel moves the session's
    stop counter alone and writes no Stop card on this slot, so the row the
    ladder wrote when that Stop ended its backoff is the only record of it here.
    """
    if not is_turn_interrupted(messages):
        return False
    turn_rows = _newest_turn_rows(messages)
    if any(is_stop_event_row(row) or _stopped_row(row) for row in turn_rows):
        return False
    row = _newest_error_row(turn_rows)
    if row is None or _turn_failed_row(row):
        return False
    kind = _row_kind(row)
    if kind == TRANSIENT_NOTICE_GIVE_UP:
        return True
    if kind == _RESTART_INTERRUPTION_KIND:
        return False
    return kind in RETRYABLE_ERROR_KINDS


def _session_stop_count(state: Any, slot: Any) -> int:
    """The session manager's Stop count for the session this chat's turns run on.

    Every surface that can stop a session moves it (``SessionManager.stop_turn``,
    a channel's ``note_stop``): the dashboard's Stop handler, Slack's ``!stop``, a
    transport's stop verb. A Stop issued on a linked channel against an IDLE chat
    moves nothing else -- no Stop card, no slot epoch, no task cancel -- so both
    passes read this count when a resume is armed and compare it before they
    dispatch. The manager records a Stop only for a key with a live session, and
    a chat with a resume pending has none (a restored one before its first turn;
    one whose failed turn tore its session down), so each pass ARMS the key first
    (:func:`_arm_stops`) and the count moves for it anyway. Read defensively, as
    the runner's own Stop read does: a state without a session manager, a double
    without the method, or a non-int answer reads as 0. (Not imported from the
    runner's owner modules: the runner is their one import path.)
    """
    reader = getattr(getattr(state, "sessions", None), "stop_generation", None)
    if not callable(reader):
        return 0
    try:
        value = reader(effective_session_key(slot))
    except Exception:  # pragma: no cover - a broken double, not a stop
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _arm_stops(state: Any, slot: Any) -> None:
    """Keep a Stop on this chat's session recordable while a resume is armed.

    ``SessionManager.arm_resume``; paired with :func:`_disarm_stops` once the
    resume dispatched or stood down. A manager without the method (a double)
    is left alone, like the count read.
    """
    arm = getattr(getattr(state, "sessions", None), "arm_resume", None)
    if callable(arm):
        arm(effective_session_key(slot))


def _disarm_stops(state: Any, slot: Any) -> None:
    disarm = getattr(getattr(state, "sessions", None), "disarm_resume", None)
    if callable(disarm):
        disarm(effective_session_key(slot))


@contextlib.contextmanager
def stops_recordable_through_teardown(state: Any, slot: Any) -> Iterator[None]:
    """Hold this chat's key armed across its turn's teardown, until the error pass has decided.

    The runner's bracket around ``_turn_release_session`` and the turn tail,
    whose last step is :func:`schedule_after_error`. ``reset`` pops the live
    session before its teardown awaits -- the end record, the unlink, the child
    probes, the provider shutdown, seconds on a slow host -- and the manager
    records a channel Stop only for a live session, an open replay gap or an
    armed key, so a Slack ``!stop`` landing after the pop was recorded nowhere:
    the pass's comparison against the turn's entry reading passed and the chat
    resumed a turn the person had stopped (GPT review, rev 72). The same
    counted arm the passes use (:func:`_arm_stops`), so the pass's own arm
    stacks on this one and is released on the pass's own schedule. Nothing is
    held while the switch is off: that path stays as it was. Never raises into
    the span it brackets -- the session permit's release runs inside it.
    """
    held = False
    try:
        held = _switch_on_now() is not False
        if held:
            _arm_stops(state, slot)
    except Exception:  # noqa: BLE001 - a hold that cannot be taken must not skip the release
        held = False
    try:
        yield
    finally:
        if held:
            _disarm_stops(state, slot)


def arm_cut_chats(state: Any) -> int:
    """Arm every chat the restart cut, the moment it exists; returns how many.

    Synchronous -- no await between a chat's arming and its Stop-count reading,
    which lands on the slot (``_restart_stop_count``) for the boot pass to
    compare against. Called by the restore right after the open tabs are
    rebuilt, by :func:`start_boot_pass` before the boot task exists, and by the
    pass itself: a channel Stop can land during any of the awaits between those
    points (the recent-session restore, the approval-mode notice pass's
    deferred transcript loads) and would otherwise be recorded nowhere -- the
    chat has no live session yet. Once per chat: an armed chat is left as it
    is, so its first reading stands.
    """
    armed = 0
    for slot in list(state._slots.values()):
        if getattr(slot, "_restart_stop_count", None) is not None:
            continue
        if not getattr(slot, "_restart_cut_turn", False) or not _eligible_slot(slot):
            continue
        _arm_stops(state, slot)
        slot._restart_stop_count = _session_stop_count(state, slot)
        armed += 1
    return armed


def _disarm_cut_chat(state: Any, slot: Any) -> None:
    """Undo :func:`arm_cut_chats` for one chat; a chat not armed is left alone."""
    if getattr(slot, "_restart_stop_count", None) is None:
        return
    slot._restart_stop_count = None
    _disarm_stops(state, slot)


def disarm_cut_chats(state: Any, *extra: Any) -> None:
    """Disarm every chat :func:`arm_cut_chats` armed (plus *extra* slots a pass
    saw replaced or closed, which the state's slot map does not hold)."""
    for slot in [*state._slots.values(), *extra]:
        _disarm_cut_chat(state, slot)


# ── Dispatch ───────────────────────────────────────────────────────────────────


def _resume_body(cause: str) -> str:
    return _AUTO_RESUME_RESTART_MSG if cause == CAUSE_RESTART else _AUTO_RESUME_ERROR_MSG


async def _dispatch(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    cause: str,
    limit: int,
    window_secs: float,
    stop_count: int | None = None,
) -> bool:
    """Queue and start one automatic resume, or refuse. Returns whether it ran.

    The stamp is spent first and written to the metadata line before anything
    is queued (:func:`_commit_budget`): no automatic provider work starts on a
    budget that exists only in memory, and a write that did not complete
    refuses the resume. The person's-action check and a last read of the switch
    then run under the slot lock right before the queue insert
    (``before_dispatch``): the last point where refusing still changes nothing
    -- a cancel that lands between the backoff and the insert cannot leave a
    gateway-authored entry at the head of the queue, and a switch turned off
    while the budget write was in flight (which moves no epoch) cannot start a
    turn. The hook also compares the session's Stop count against the one the
    pass captured when it armed this resume (*stop_count*): a Stop issued on a
    linked channel moves that count and nothing on the slot, so it is the only
    trace such a Stop leaves. A refusal there, or any of ``continue_slot_turn``'s own (running,
    queued, approval pending, children running, two failed starts, a turn that
    finished meanwhile), dispatched nothing, so the stamp is taken back in
    memory (:func:`_unstamp`); the line carries it until the next save.
    """
    # Imported here: ``chat_handlers`` binds this module at import time for its
    # ``chat_api`` owners, so a module-level import back would be a cycle.
    from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal, continue_slot_turn

    now = time.time()
    used = budget_used(slot, window_secs=window_secs, now=now)
    if used >= limit:
        _notice_budget_exhausted(state, slot, used=used, limit=limit, window_secs=window_secs)
        return False
    attempt = used + 1
    epoch = slot._auto_resume_epoch
    slot._auto_resume_stamps.append(now)
    slot._dirty = True
    if not await _commit_budget(state, slot):
        _unstamp(slot, now)
        if slot._auto_resume_epoch != epoch:
            logger.info(
                "restart_resume: slot %s not resumed after %s: a person acted on the chat "
                "while its budget was being written",
                slot.key,
                cause,
            )
        else:
            logger.warning(
                "restart_resume: slot %s not resumed after %s: its budget could not be "
                "written before the resume",
                slot.key,
                cause,
            )
        return False

    def _refuse_if_superseded() -> None:
        if slot._auto_resume_epoch != epoch:
            raise SlotContinueRefusal("a person acted on the chat", "auto_resume_superseded")
        # The switch once more, under the same lock. Turning it off moves no
        # epoch, and the budget write awaited above is a window in which it can
        # have been turned off since the pass last read it.
        if _switch_on_now() is False:
            raise SlotContinueRefusal(
                "automatic resume is switched off", "auto_resume_switched_off"
            )
        if stop_count is not None and _session_stop_count(state, slot) != stop_count:
            raise SlotContinueRefusal("the person stopped the session", "auto_resume_stopped")

    try:
        await continue_slot_turn(
            state,
            slot,
            directive_user_origin=False,
            via=VIA_RESTART if cause == CAUSE_RESTART else VIA_ERROR,
            require_interrupted=True,
            body=_resume_body(cause),
            before_dispatch=_refuse_if_superseded,
            extra_meta={"autoResume": {"cause": cause, "attempt": attempt, "limit": limit}},
        )
    except SlotContinueRefusal as exc:
        _unstamp(slot, now)
        logger.info("restart_resume: slot %s not resumed (%s): %s", slot.key, exc.code, exc.message)
        return False
    logger.info(
        "restart_resume: resumed slot %s after %s (attempt %d of %d in %d min)",
        slot.key,
        cause,
        attempt,
        limit,
        int(window_secs // 60),
    )
    return True


def _notice_budget_exhausted(
    state: DashboardState, slot: _ChatSlot, *, used: int, limit: int, window_secs: float
) -> None:
    """One notice row and one notification when the chat's budget is spent.

    Idempotent per budget: nothing is added while a pause notice already stands
    with no automatic resume after it (:func:`_pause_notice_stands`), so a crash
    loop that reaches exhaustion on every boot, or an unattended turn that fails
    again while the budget is spent, does not stack rows. A count of
    :data:`MAX_STAMPS` or more can only be the sentinel ``read_persisted_stamps``
    substitutes for a list this gateway did not write (it never appends past the
    limit, whose ceiling is far below ``MAX_STAMPS``), so that case states the
    pause and its length and nothing about the record (the codec's warning carries
    the cause; the person cannot act on it) instead of quoting the sentinel; a count merely
    above the limit is a limit the person lowered, so that case quotes the new
    limit rather than an overrun the person never chose.
    """
    if _pause_notice_stands(slot):
        return
    window = _window_label(window_secs)
    if used >= MAX_STAMPS:
        # The sentinel: the record this gateway did not write is already in the
        # log (the codec's warning names the slot and the shape), and nothing the
        # person could do answers "why", so the row states only the pause and
        # leads to the remedy. No duration either: the sentinel stamps age out
        # after one window, but that only re-allows automatic resumes for LATER
        # interruptions -- nothing re-arms this chat's cut-off turn -- so "for
        # 1h" would read as a promise to pick it back up in an hour.
        reason = ""
        happened = "automatic resume is paused"
    elif used > limit:
        reason = f"the limit is now {limit} resumes in {window}"
        happened = f"automatic resume is paused at its new limit of {limit} resumes in {window}"
    else:
        reason = f"its limit of {limit} resumes in {window} is spent"
        happened = f"automatic resume used all {limit} resumes in {window}"
    text = (
        "⚠️ Automatic resume is paused here."
        if used >= MAX_STAMPS
        else f"⚠️ Automatic resume is paused here: {reason}."
    )
    # The row sits under the error row it answers, which already carries the
    # Resume button and its own "try again"; repeating the press here made the
    # two rows read as two problems with one shared button, so this row adds only
    # what that press does besides -- and names the count, since this row is all
    # a reader arriving from the notification sees of the budget.
    slot.append(
        "assistant",
        f"{text} Pressing Resume also resets this chat's automatic-resume count.",
        "msg msg-a",
        meta={"kind": AUTO_RESUME_PAUSED_KIND},
    )
    # The notification body is read in the feed, glued to the chat's title and
    # cut off after a few words, so it names what happened before anything else;
    # the row's "its" has "here" for a referent and would dangle after a title.
    try:
        state.notify(
            "agent",
            "Automatic resume paused",
            f"{slot.title or slot.key}: {happened}. Press Resume in the chat to continue.",
            meta={"slot": slot.key, "used": used, "limit": limit},
        )
    except Exception:
        logger.debug("restart_resume: exhaustion notification failed", exc_info=True)
    _audit(slot, "restart_resume:budget_exhausted", f"{used}/{limit}")


def _window_label(window_secs: float) -> str:
    """The window spelled out for prose: ``5 minutes``, ``1 hour``, ``24 hours``.

    Whole hours read as hours, anything else as minutes -- the same split the
    Settings select makes -- but with the unit as a word: inside a sentence the
    select's ``1h`` reads like a typo (UX review), and the select itself keeps
    its abbreviations.
    """
    minutes = int(window_secs // 60)
    if minutes and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


def _pause_notice_stands(slot: Any) -> bool:
    """Whether a pause notice is already the last word on this chat's budget.

    Walks back from the newest row: a pause notice met before any automatic
    resume row (an ``inject`` carrying ``meta.autoResume``) still describes the
    current budget, whatever rows the person or an unattended turn added after
    it; an automatic resume met first means the budget has been spent anew since.
    Transcript-based, so it holds across a restart.
    """
    for row in reversed(slot.messages):
        meta = row.get("meta")
        if not isinstance(meta, dict):
            continue
        if meta.get("kind") == AUTO_RESUME_PAUSED_KIND:
            return True
        if row.get("role") == "inject" and isinstance(meta.get("autoResume"), dict):
            return False
    return False


def _newest_status_row_is(slot: Any, kind: str) -> bool:
    """Whether the newest row after the last conversational one is the *kind* notice."""
    for row in reversed(slot.messages):
        meta = row.get("meta")
        if isinstance(meta, dict) and meta.get("kind") == kind:
            return True
        if is_system_notice(row.get("role"), meta):
            continue
        if row.get("role") in ("user", "assistant", "inject", "error") and row.get("content"):
            return False
    return False


def _audit(slot: Any, operation: str, outcome: str) -> None:
    try:
        from kiro_crew.sel import sel  # imported here: the SEL is optional and heavy

        sel().log_api_access(
            caller="dashboard:restart_resume",
            operation=operation,
            outcome=outcome,
            resources=slot.key,
        )
    except Exception:
        logger.debug("restart_resume: SEL audit failed for %s", operation, exc_info=True)


# ── The boot pass ──────────────────────────────────────────────────────────────


def _eligible_slot(slot: Any) -> bool:
    """A local, live, persistent chat the person owns the turns of.

    A relay archive never runs; a closing chat has no turn to pick up; a chat an
    app owns (``slot._app``) is the app's to continue -- it re-grants its own
    trust and resumes on its own terms, and a gateway-authored continuation would
    run behind its back; a chat in a restricted memory mode (incognito,
    temporary) is never acted on without the person -- the automation would
    write rows into a transcript they chose to keep private.
    """
    if is_relay_archive(slot):
        return False
    if getattr(slot, "is_closing", False) or getattr(slot, "_app", ""):
        return False
    return getattr(slot, "memory_mode", "persistent") == "persistent"


async def resume_after_restart(state: DashboardState, cfg: KiroCrewConfig) -> int:
    """Resume every restored chat the restart cut off. Returns how many were dispatched.

    Run by the boot task after the notice pass, once the startup restore has
    built the slots. Returns at once when the switch is off. Only chats whose metadata line
    carried a turn-in-flight marker are looked at (the restore's first reading
    of it), so a deferred sidebar window is loaded -- off the loop -- for those
    alone; the candidate test then runs on the loaded, reconciled rows. The
    cautious-boot pause and :data:`BOOT_SETTLE_SECS` run before the first
    dispatch, :data:`BOOT_STAGGER_SECS` between chats, and one chat's failure
    is logged and skipped rather than ending the pass. A chat the person
    touches before its turn comes up is skipped: their send or Resume clears the
    marker flag the candidate test re-reads (``reset_budget``), a Stop card ends
    the turn on purpose, and an action that lands after the dispatch began is
    caught by the epoch it moved, under the lock. The switch is read again from
    the live snapshot before each dispatch, so turning it off during the pause,
    the settle or the stagger ends the pass.
    """
    enabled, limit, window_secs = _settings(cfg)
    if not enabled:
        return 0
    # Imported here: ``cautious_boot`` is a server-runtime concern this module
    # otherwise has no reason to load on the codec's restore path.
    from kiro_crew.dashboard import cautious_boot

    candidates: list[_ChatSlot] = []
    # Each candidate's session Stop count as read when its key was ARMED -- by the
    # restore the moment the chat existed, or here for a chat that appeared
    # since: a Stop from a linked channel during the restore's later awaits, the
    # notice pass, the window load, the pause, the settle or the stagger moves
    # the count and nothing else on an idle chat, and the manager records it
    # only because the key is armed. Every armed key is disarmed when the pass
    # ends, however it ends.
    arm_cut_chats(state)
    stop_counts: dict[str, int] = {
        s.key: count
        for s in state._slots.values()
        if (count := getattr(s, "_restart_stop_count", None)) is not None
    }
    armed: list[_ChatSlot] = [s for s in state._slots.values() if s.key in stop_counts]
    try:
        for slot in list(armed):
            if not getattr(slot, "_restart_cut_turn", False) or not _eligible_slot(slot):
                continue
            stop_count = stop_counts[slot.key]
            try:
                await load_window_off_loop(slot)
            except Exception:
                logger.warning("restart_resume: window load failed for %s", slot.key, exc_info=True)
                continue
            if state._slots.get(slot.key) is not slot:
                continue
            if _session_stop_count(state, slot) != stop_count:
                logger.info(
                    "restart_resume: slot %s not resumed: the person stopped the session", slot.key
                )
                slot._restart_cut_turn = False
                continue
            if restart_candidate(slot):
                candidates.append(slot)
            else:
                # Examined and not resumable: this boot's reading is spent for it too.
                slot._restart_cut_turn = False
        if not candidates:
            return 0
        logger.info("restart_resume: %d chat(s) were cut off by the restart", len(candidates))
        await cautious_boot.pause_before("restart resume")
        await asyncio.sleep(BOOT_SETTLE_SECS)
        dispatched = 0
        for index, slot in enumerate(candidates):
            if index:
                await asyncio.sleep(BOOT_STAGGER_SECS)
            if _switch_on_now() is False:
                logger.info("restart_resume: switch turned off during the boot pass; stopping")
                for left in candidates[index:]:
                    # This boot's reading is spent for them too: the marker was consumed
                    # by the reconcile and a later pass must not re-read it.
                    left._restart_cut_turn = False
                break
            if state._slots.get(slot.key) is not slot or not restart_candidate(slot):
                # Replaced, closed, or the person got there first.
                continue
            if _session_stop_count(state, slot) != stop_counts[slot.key]:
                # A Stop from a linked channel while the pass waited: the chat is
                # left alone, and this boot's reading is spent for it (``finally``
                # does not run for a skipped chat, so clear it here).
                logger.info(
                    "restart_resume: slot %s not resumed: the person stopped the session", slot.key
                )
                slot._restart_cut_turn = False
                continue
            try:
                if await _dispatch(
                    state,
                    slot,
                    cause=CAUSE_RESTART,
                    limit=limit,
                    window_secs=window_secs,
                    stop_count=stop_counts[slot.key],
                ):
                    dispatched += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("restart_resume: boot pass failed for %s", slot.key, exc_info=True)
            finally:
                # This boot's reading is spent either way; a later pass must not re-read it.
                slot._restart_cut_turn = False
        return dispatched
    finally:
        disarm_cut_chats(state, *armed)


async def _boot(state: DashboardState, cfg: KiroCrewConfig) -> int:
    """The notice pass, then the resume pass: what the boot task runs."""
    await notice_dropped_approval_modes(state, cfg)
    return await resume_after_restart(state, cfg)


def start_boot_pass(state: DashboardState, cfg: KiroCrewConfig) -> asyncio.Task[int]:
    """Run the two restore-time passes in the background, in order.

    First :func:`notice_dropped_approval_modes`, then :func:`resume_after_restart`;
    each returns at once when the switch is off. Both read transcripts the
    restore deliberately deferred -- one load per chat that was on a grant, one
    per chat the restart cut -- so neither belongs on the path ``KIROCREW_READY``
    waits on: the gateway listens first, and the passes catch up behind it, the
    notice landing before any automatic dispatch could run a turn under a mode
    the person does not yet know about.
    The cut chats' keys are armed HERE, before the task exists (the restore has
    normally armed them already, the moment the open tabs came back): the notice
    pass awaits transcript loads before the resume pass runs, and a channel Stop
    landing there must be recorded. Disarmed when the task ends, however it ends.
    The task is tracked in ``state._background_tasks`` like every other.
    """
    arm_cut_chats(state)
    task = asyncio.create_task(_boot(state, cfg), name="restart_resume.boot")
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    task.add_done_callback(lambda _t: disarm_cut_chats(state))
    return task


# ── The error pass ─────────────────────────────────────────────────────────────


def backoff_secs(attempt: int, *, rng: random.Random | None = None) -> float:
    """Delay before the *attempt*-th (1-based) automatic resume of a chat in the window."""
    base = min(BACKOFF_BASE_SECS * (2 ** max(0, attempt - 1)), BACKOFF_CAP_SECS)
    jitter = (rng or random).random() * BACKOFF_JITTER * base
    return base + jitter


def schedule_after_error(state: DashboardState, slot: _ChatSlot) -> bool:
    """Arm one backed-off automatic resume for a turn that just ended in an error.

    Called from the runner once the slot is idle. Returns whether a retry was
    armed. Nothing is armed while the switch is off in the config snapshot the
    process holds; when no snapshot is armed yet the task reads the config off
    the loop. The switch is read AGAIN after the backoff, so turning it off
    during the wait stops the retry. Never arms twice for one chat.
    """
    if getattr(slot, "_auto_resume_task", None) is not None:
        return False
    # The switch first: with it off this runs at the end of every turn, so the
    # off path is one in-memory snapshot read, not a scan of the transcript.
    if _switch_on_now() is False:
        return False
    if not _eligible_slot(slot) or not error_retry_candidate(slot.messages):
        return False
    # The session's Stop count as the retry is armed: a Stop from a linked
    # channel during the backoff moves it and nothing else on this chat. Armed
    # first, or the manager would not record it: the failed turn may have torn
    # the session down. Disarmed when the task ends, however it ends -- a cancel
    # before its first step never runs the body's own cleanup.
    _arm_stops(state, slot)
    stop_count = _session_stop_count(state, slot)
    # Measured against the count the failed turn read at its entry
    # (``_turn_open``, on the slot): a Stop that landed while the turn ran or
    # during its teardown -- after the session permit was released, through the
    # marker clear and the chat_done frame, before this runs -- moved the
    # counter and wrote nothing here, and read only now it would pass as the
    # baseline (GPT review, rev 65). The person said stop; nothing is armed. The
    # runner holds the key armed through that teardown
    # (:func:`stops_recordable_through_teardown`), so a Stop landing after the
    # reset popped the session is recorded too (GPT review, rev 72). A turn
    # that ended before its entry snapshot leaves no retryable row, so a stale
    # reading from an earlier turn is never compared.
    entry_stop_count = getattr(slot, "_turn_entry_stop_count", None)
    if entry_stop_count is not None and stop_count != entry_stop_count:
        logger.info(
            "restart_resume: slot %s not resumed after an error: the person stopped "
            "the session during the turn or its teardown",
            slot.key,
        )
        _disarm_stops(state, slot)
        return False

    async def _run() -> None:
        try:
            enabled, limit, window_secs = await asyncio.to_thread(_load_settings)
            if not enabled:
                return
            used = budget_used(slot, window_secs=window_secs)
            if used >= limit:
                _notice_budget_exhausted(
                    state, slot, used=used, limit=limit, window_secs=window_secs
                )
                return
            await _backoff_sleep(backoff_secs(used + 1))
            enabled, limit, window_secs = await asyncio.to_thread(_load_settings)
            if not enabled:
                return
            if state._slots.get(slot.key) is not slot or not error_retry_candidate(slot.messages):
                return
            if _session_stop_count(state, slot) != stop_count:
                logger.info(
                    "restart_resume: slot %s not resumed after an error: the person stopped "
                    "the session during the backoff",
                    slot.key,
                )
                return
            # Past this point a cancel cannot stop the dispatch mid-way: a cancel
            # that landed after the queue insert would strand the committed head
            # entry undispatched. A person's action is caught by the epoch check
            # under the lock instead.
            if getattr(slot, "_auto_resume_task", None) is task:
                slot._auto_resume_task = None
            await _dispatch(
                state,
                slot,
                cause=CAUSE_ERROR,
                limit=limit,
                window_secs=window_secs,
                stop_count=stop_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("restart_resume: error pass failed for %s", slot.key, exc_info=True)
        finally:
            if getattr(slot, "_auto_resume_task", None) is task:
                slot._auto_resume_task = None

    task = asyncio.create_task(_run(), name=f"restart_resume.error:{slot.key}")
    task.add_done_callback(lambda _t: _disarm_stops(state, slot))
    slot._auto_resume_task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return True


# ── The approval-mode notice ───────────────────────────────────────────────────


async def notice_dropped_approval_modes(state: DashboardState, cfg: KiroCrewConfig) -> int:
    """Run :func:`approval_mode_dropped_notice` over every restored slot. Returns the count.

    The comparison is taken for every slot FIRST, synchronously, from the value
    the restore read off the line (``_approval_mode_recorded``, which no save
    writer touches), and only then are windows loaded -- off the loop, a
    restored sidebar row defers its transcript -- for the idempotency walk, and
    only for the slots with something to say. *cfg* is the config the restore
    loaded: a crewmate's chat is measured against the mode its profile declares,
    found through the chat's execution record the way the turn admission finds
    it (:func:`_admission_seeded_mode`, a disk read taken off the loop for the
    candidates only, before any window loads).
    Run by the boot task (:func:`start_boot_pass`), before the resume pass and
    after the gateway is listening. Returns at once when the switch is off: the
    notice is behind ``dashboard.resume_interrupted_turns`` like the pass it
    precedes, and only the record is kept (rfc-restart-resume §4.6).
    """
    if not _settings(cfg)[0]:
        return 0
    changed = [
        (slot, recorded)
        for slot in list(state._slots.values())
        if (recorded := _recorded_grant_not_held(slot, cfg, seeded="")) and _eligible_slot(slot)
    ]
    # The other direction, for the chats the resume pass would run a turn on with
    # nobody there (the restore's reading of the cut-turn marker, which this pass
    # runs before and does not consume): the person last saw ``normal`` and the
    # admission seed puts a grant back as that turn is admitted. A chat nobody
    # resumes is left out -- its next turn is the person's own, seeded under
    # their eyes, as every turn of a mate's chat is today.
    raised_candidates = [
        slot
        for slot in list(state._slots.values())
        if getattr(slot, "_restart_cut_turn", False)
        and (getattr(slot, "_approval_mode_recorded", "") or "") == "normal"
        and _eligible_slot(slot)
    ]
    seeded = await asyncio.to_thread(
        lambda: {
            slot.key: _admission_seeded_mode(cfg, slot)
            for slot in [s for s, _ in changed] + raised_candidates
        }
    )
    told = 0
    for slot, recorded in changed:
        if seeded.get(slot.key) == recorded:
            continue
        try:
            await load_window_off_loop(slot)
            if state._slots.get(slot.key) is not slot:
                continue
            if approval_mode_dropped_notice(
                slot, recorded=recorded, seeded=seeded.get(slot.key, "")
            ):
                told += 1
        except Exception:
            logger.debug(
                "restart_resume: approval-mode notice failed for %s", slot.key, exc_info=True
            )
    for slot in raised_candidates:
        grant = _seeded_grant_not_seen(slot, seeded.get(slot.key, ""))
        if not grant:
            continue
        try:
            await load_window_off_loop(slot)
            if state._slots.get(slot.key) is not slot:
                continue
            if approval_mode_raised_notice(slot, seeded=grant):
                told += 1
        except Exception:
            logger.debug(
                "restart_resume: approval-mode notice failed for %s", slot.key, exc_info=True
            )
    return told


def _recorded_grant_not_held(
    slot: Any, cfg: KiroCrewConfig | None, *, seeded: str | None = None
) -> str:
    """The recorded mode when it was a grant the slot does not hold now, else ``""``.

    Nothing to say when the record is absent or ``normal``; when the mode came
    back (crash-only trust once it lands); for an app-owned chat, whose app owns
    the grant and re-grants it (the passes never resume such a chat either); for
    a crewmate's chat whose recorded mode is the one its profile declares, which
    ``crewmate_approval.seed_for_turn`` re-applies as each of its turns is
    admitted, before the provider is claimed -- the turn the boot pass resumes
    included, so that turn runs under the mode the person last saw; a chat whose
    grant was not the declared one (an approval card's "trust this session") is
    always told; or when the process-global override is live, because under
    ``agent.dangerously_skip_permissions`` nothing asks and "now on Ask every
    time" would be false.

    ``seeded`` is the mode that admission seed puts back, ``""`` when nothing
    seeds; the pass resolves it from the chat's execution record off the loop
    (:func:`_admission_seeded_mode`). Left ``None``, a crewmate thread is measured
    against the mode its profile declares for the alias the thread carries
    (:func:`_declared_member_mode`), the reading a single call can take on the
    loop; any other chat is read as seeded nothing.
    """
    recorded = getattr(slot, "_approval_mode_recorded", "") or ""
    if recorded in ("", "normal") or live_approval_mode(slot) == recorded:
        return ""
    if getattr(slot, "_app", ""):
        return ""
    if seeded is None:
        seeded = _seed_read_on_the_loop(cfg, slot)
    if seeded == recorded:
        return ""
    # Imported here: the override module reads config state this module must not
    # pull in at import time (the codec imports this module on the restore path).
    from kiro_crew.safety_override import safety_override

    if safety_override().is_active():
        return ""
    return recorded


def _seed_read_on_the_loop(cfg: KiroCrewConfig | None, slot: Any) -> str:
    """The admission seed a single call can read on the loop: a crewmate thread's declared mode.

    Any other chat reads as seeded nothing; the pass reads the execution record
    off the loop instead (:func:`_admission_seeded_mode`) and passes what it found.
    """
    return _declared_member_mode(cfg, slot) if getattr(slot, "mode", "") == DM_SLOT_MODE else ""


def _seeded_grant_not_seen(slot: Any, seeded: str) -> str:
    """The grant the admission seed puts back when the person last saw ``normal``, else ``""``.

    The inverse of :func:`_recorded_grant_not_held`. Nothing to say when the
    record is absent (it says nothing about what was seen, so nothing is
    claimed) or names a grant (the dropped direction, or nothing changed); when
    nothing seeds (a plain chat's resumed turn waits at its first approval card
    like any interactive turn); for an app-owned chat; or under the process-global
    override, where nothing asks either way. ``seeded`` is what the admission
    seed applies (:func:`_admission_seeded_mode`, read off the loop).
    """
    recorded = getattr(slot, "_approval_mode_recorded", "") or ""
    if recorded != "normal" or seeded not in ("trust", "trust_reads"):
        return ""
    if getattr(slot, "_app", ""):
        return ""
    from kiro_crew.safety_override import safety_override

    if safety_override().is_active():
        return ""
    return seeded


def _admission_seeded_mode(cfg: KiroCrewConfig | None, slot: Any) -> str:
    """The approval mode the turn admission seeds on *slot*'s next turn, else ``""``.

    Decided the way ``crewmate_approval.seed_for_turn`` decides it: the session's
    attested execution record names the crewmate (its conductor chat or one of
    its workers, through ``crewmate_name_for``), and that crewmate's declared
    ``approval_mode`` is the seed, an unset one reading as Trust. A record that
    cannot be read admits no turn, and one that names no crewmate seeds nothing.
    Reads the record from disk: call it off the loop.
    """
    from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
    from kiro_crew.execution_context import read_session_execution

    try:
        execution = read_session_execution(effective_session_key(slot))
    except Exception:  # noqa: BLE001 - an unreadable record admits no turn, so nothing seeds
        return ""
    name = crewmate_name_for(cfg, execution)
    if not name:
        return ""
    record = getattr(cfg, "agents", {}).get(name)
    return getattr(record, "approval_mode", "") or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE


def _declared_member_mode(cfg: KiroCrewConfig | None, slot: Any) -> str:
    """The approval mode a crewmate's profile declares for its thread, as the seed reads it.

    ``slot.agent`` names the crewmate on a DM thread. An unset mode resolves the way
    ``_seed_member_approval`` resolves it (``MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE``),
    and so does a config that could not be read, since the seed will apply that
    default. A config that was read and holds no record for the crewmate (renamed,
    removed) returns ``""``: the open-thread route seeds nothing then, so no mode
    comes back.
    """
    agents = getattr(cfg, "agents", None)
    if not isinstance(agents, dict):
        return MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
    crew = agents.get(getattr(slot, "agent", ""))
    if crew is None:
        return ""
    return getattr(crew, "approval_mode", "") or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE


def approval_mode_dropped_notice(
    slot: _ChatSlot,
    *,
    recorded: str | None = None,
    cfg: KiroCrewConfig | None = None,
    seeded: str | None = None,
) -> bool:
    """Tell the person when a restored chat holds a different mode than they last saw.

    ``recorded`` is what the periodic flush wrote from the live flags before the
    process went down (read off the slot when not given, against *cfg* for a
    crewmate thread; the restore wrapper passes the value it read before any
    window load). The slot comes back
    interactive because trust is in memory by construction. When the two differ,
    one assistant-role system notice says so and names the remedy. Returns
    whether a row was added. Idempotent: nothing is added when the newest status
    row already is this notice, so a second restart before the person acts adds
    nothing.

    ``seeded`` is the grant the turn admission puts back as the chat's next turn
    is admitted (the pass resolves it from the execution record off the loop;
    ``None`` reads a crewmate thread's declared mode on the loop). When it is a
    grant, "now on" names it, not the interactive mode the restored slot holds
    for the moment before that turn: a mate's chat the person had set to Reads
    comes back running on its profile's Trust, the resumed turn included, and a
    notice reading "now on Ask every time" would be wrong in the risky direction
    (GPT review, rev 71).
    """
    if recorded is None:
        recorded = _recorded_grant_not_held(slot, cfg, seeded=seeded)
    if not recorded:
        return False
    if seeded is None:
        seeded = _seed_read_on_the_loop(cfg, slot)
    current = seeded if seeded in _APPROVAL_MODE_OPTIONS else live_approval_mode(slot)
    if current == recorded or _newest_status_row_is(slot, APPROVAL_MODE_DROPPED_KIND):
        return False
    # The remedy names the control the way the screen labels it ("Approval mode",
    # under the message box) and the option by its own name; "composer" is the
    # codebase's word and appears nowhere a person can read.
    if seeded in _APPROVAL_MODE_OPTIONS:
        text = (
            "This chat's permissions changed when the app restarted: it was on "
            f"{_APPROVAL_MODE_LABELS.get(recorded, recorded)}, and its turns now run on "
            f"{_APPROVAL_MODE_LABELS.get(seeded, seeded)}, the mode its profile declares. "
            f"To keep {_APPROVAL_MODE_OPTIONS.get(recorded, recorded)}, set it in the profile."
        )
    else:
        remedy = (
            "Open this chat from the Mates page to re-apply the mode its profile "
            "declares; set that mode in the profile to keep it across restarts."
            if getattr(slot, "mode", "") == DM_SLOT_MODE
            else (
                f"To turn it back on, choose {_APPROVAL_MODE_OPTIONS.get(recorded, recorded)} "
                "in Approval mode under the message box."
            )
        )
        text = (
            "This chat's permissions changed when the app restarted: it was on "
            f"{_APPROVAL_MODE_LABELS.get(recorded, recorded)} and is now on "
            f"{_APPROVAL_MODE_LABELS.get(current, current)}. {remedy}"
        )
    # A live append, like the pause notice's: the boot task runs after the
    # gateway is listening, so a tab that reconnected first is watching, and a
    # replay-mode append (``broadcast=False``) would store the row for the next
    # transcript reload while the resumed turn -- and its approval card --
    # arrived live, unexplained, ahead of it.
    slot.append(
        "assistant",
        text,
        "msg msg-a",
        meta={"kind": APPROVAL_MODE_DROPPED_KIND, "from": recorded, "to": current},
    )
    slot._dirty = True
    _audit(slot, "restart_resume:approval_mode_dropped", f"{recorded}->{current}")
    return True


def approval_mode_raised_notice(slot: _ChatSlot, *, seeded: str) -> bool:
    """Tell the person a mate's chat they last saw on Ask every time runs on its profile's grant.

    The other direction of :func:`approval_mode_dropped_notice`, for a chat the
    boot pass is about to resume: the person set the mode in the composer, which
    lives in memory, and the restart put the profile's declared grant (*seeded*)
    back as each turn is admitted -- the resumed one included, so its tools run
    without asking with nobody there (rfc-restart-resume §4.6). The pass seeds
    and skips nothing; it says so, before it resumes, under the same row kind
    (``meta.from`` / ``meta.to`` carry the direction) and with the same
    idempotency. Returns whether a row was added.
    """
    if _newest_status_row_is(slot, APPROVAL_MODE_DROPPED_KIND):
        return False
    text = (
        "This chat's permissions changed when the app restarted: it was on Ask every "
        f"time, and its turns now run on {_APPROVAL_MODE_LABELS.get(seeded, seeded)}, the "
        "mode its profile declares -- the automatic resume included. To keep it asking, "
        "set Ask every time in the profile."
    )
    slot.append(
        "assistant",
        text,
        "msg msg-a",
        meta={"kind": APPROVAL_MODE_DROPPED_KIND, "from": "normal", "to": seeded},
    )
    slot._dirty = True
    _audit(slot, "restart_resume:approval_mode_raised", f"normal->{seeded}")
    return True
