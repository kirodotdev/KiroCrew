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

Both passes are inert while ``dashboard.resume_interrupted_turns`` is off (the
default): each reads the switch before it arms or dispatches, and the error pass
reads it again after its backoff. The record of what the person last saw and
the notice are not behind the switch -- a chat that came back under a different
mode is told so whether or not the gateway may resume it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import random
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from kiro_crew.config.sections import (
    MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE,
    RESUME_RETRY_LIMIT_MIN,
    RESUME_RETRY_WINDOW_MIN,
)
from kiro_crew.dashboard.chat_utils import (
    _AUTO_RESUME_ERROR_MSG,
    _AUTO_RESUME_RESTART_MSG,
    RETRY_INVITED_KIND,
    SESSION_START_FAILED_KIND,
    TRANSIENT_NOTICE_GIVE_UP,
    TRANSIENT_NOTICE_META_KEY,
    TRANSIENT_RETRY_KIND,
    TURN_FAILED_META,
    run_to_completion,
    slot_history_key,
)
from kiro_crew.dashboard.relay_archive import is_relay_archive
from kiro_crew.dashboard.slot_persistence.metadata_codec import live_approval_mode
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

#: The persisted stamp list is bounded well above any limit the config allows
#: (20), so a list longer than this is not something this gateway wrote.
MAX_STAMPS = 64

#: A stamp this far in the future is not a clock skew this gateway produced.
_FUTURE_SLACK_SECS = 300.0

#: Seam for the error-pass backoff, so a test can hold the task inside its wait
#: without a wall-clock sleep (the same shape as the runner's ``_recovery_delay``).
_backoff_sleep = asyncio.sleep

_APPROVAL_MODE_LABELS = {
    "trust": "Trust (tools run without asking)",
    "trust_reads": "Trust reads (read-only commands run without asking)",
    "normal": "Ask every time",
}


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


def read_persisted_stamps(raw: object, *, now: float | None = None) -> tuple[list[float], bool]:
    """Validate a metadata-line ``auto_resume_stamps`` value: ``(stamps, valid)``.

    The metadata line is a writable file, so every element is re-checked. Anything
    that is not a list of finite, non-negative, non-future floats within
    :data:`MAX_STAMPS` -- including an integer too large to be a float, which
    ``float()`` refuses with ``OverflowError`` -- reads as a FULL list:
    ``MAX_STAMPS`` copies of *now*, which makes the budget refuse the automation
    for one window from now (the fail-closed direction for an automatic
    dispatcher; a person's send or Resume clears it sooner) and never touches the
    manual Resume. Never raises on a bad value: this runs inside the slot restore,
    where an exception would turn a tampered line into a chat that cannot be
    opened. ``valid`` is False for the bad case, so the reader can say which chat
    and why.
    """
    if raw is None:
        return [], True
    current = time.time() if now is None else now
    full = [current] * MAX_STAMPS
    if not isinstance(raw, list) or len(raw) > MAX_STAMPS:
        return full, False
    stamps: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return full, False
        try:
            value = float(item)
        except (OverflowError, ValueError):
            return full, False
        if not math.isfinite(value) or value < 0 or value > current + _FUTURE_SLACK_SECS:
            return full, False
        stamps.append(value)
    return stamps, True


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
    """
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
    continues on its own, including the give-up row the ladder writes when a
    Stop lands during its backoff.
    """
    if not is_turn_interrupted(messages):
        return False
    turn_rows = _newest_turn_rows(messages)
    if any(is_stop_event_row(row) for row in turn_rows):
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
) -> bool:
    """Queue and start one automatic resume, or refuse. Returns whether it ran.

    The stamp is spent first and written to the metadata line before anything
    is queued (:func:`_commit_budget`): no automatic provider work starts on a
    budget that exists only in memory, and a write that did not complete
    refuses the resume. The person's-action check then runs under the slot lock
    right before the queue insert (``before_dispatch``): the last point where
    refusing still changes nothing -- a cancel that lands between the backoff
    and the insert cannot leave a gateway-authored entry at the head of the
    queue. A refusal there, or any of ``continue_slot_turn``'s own (running,
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
    limit, whose ceiling is far below ``MAX_STAMPS``), so that case says the
    record could not be read instead of quoting the sentinel; a count merely
    above the limit is a limit the person lowered, and both numbers are quoted
    so the two do not read as a contradiction.
    """
    if _pause_notice_stands(slot):
        return
    window = _window_label(window_secs)
    if used >= MAX_STAMPS:
        reason = (
            "the record of earlier automatic resumes on disk could not be read, so "
            f"none is attempted for the next {window}"
        )
    elif used > limit:
        reason = f"{used} resumes in the last {window}, over its limit of {limit}"
    else:
        reason = f"its limit of {limit} resumes in {window} is spent"
    slot.append(
        "assistant",
        f"⚠️ Automatic resume paused for this chat: {reason}. Press Resume to continue; "
        "that also resets the count.",
        "msg msg-a",
        meta={"kind": AUTO_RESUME_PAUSED_KIND},
    )
    try:
        state.notify(
            "agent",
            "Automatic resume paused",
            f"{slot.title or slot.key}: {reason}. Press Resume in the chat to continue.",
            meta={"slot": slot.key, "used": used, "limit": limit},
        )
    except Exception:
        logger.debug("restart_resume: exhaustion notification failed", exc_info=True)
    _audit(slot, "restart_resume:budget_exhausted", f"{used}/{limit}")


def _window_label(window_secs: float) -> str:
    """The window the way the Settings select names it: ``5m``, ``1h``, ``24h``.

    Whole hours read as hours, anything else as minutes, so the notice and the
    control the person set it on say the same thing.
    """
    minutes = int(window_secs // 60)
    return f"{minutes // 60}h" if minutes and minutes % 60 == 0 else f"{minutes}m"


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
    for slot in list(state._slots.values()):
        if not getattr(slot, "_restart_cut_turn", False) or not _eligible_slot(slot):
            continue
        try:
            await load_window_off_loop(slot)
        except Exception:
            logger.warning("restart_resume: window load failed for %s", slot.key, exc_info=True)
            continue
        if state._slots.get(slot.key) is not slot:
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
        try:
            if await _dispatch(
                state, slot, cause=CAUSE_RESTART, limit=limit, window_secs=window_secs
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


async def _boot(state: DashboardState, cfg: KiroCrewConfig) -> int:
    """The notice pass, then the resume pass: what the boot task runs."""
    await notice_dropped_approval_modes(state, cfg)
    return await resume_after_restart(state, cfg)


def start_boot_pass(state: DashboardState, cfg: KiroCrewConfig) -> asyncio.Task[int]:
    """Run the two restore-time passes in the background, in order.

    First :func:`notice_dropped_approval_modes`, which always runs, then
    :func:`resume_after_restart`, which returns at once when the switch is off.
    Both read transcripts the restore deliberately deferred -- one load per chat
    that was on a grant, one per chat the restart cut -- so neither belongs on
    the path ``KIROCREW_READY`` waits on: the gateway listens first, and the
    passes catch up behind it, the notice always landing before any automatic
    dispatch could run a turn under a mode the person does not yet know about.
    The task is tracked in ``state._background_tasks`` like every other.
    """
    task = asyncio.create_task(_boot(state, cfg), name="restart_resume.boot")
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
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
    if not _eligible_slot(slot) or not error_retry_candidate(slot.messages):
        return False
    if _switch_on_now() is False:
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
            # Past this point a cancel cannot stop the dispatch mid-way: a cancel
            # that landed after the queue insert would strand the committed head
            # entry undispatched. A person's action is caught by the epoch check
            # under the lock instead.
            if getattr(slot, "_auto_resume_task", None) is task:
                slot._auto_resume_task = None
            await _dispatch(state, slot, cause=CAUSE_ERROR, limit=limit, window_secs=window_secs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("restart_resume: error pass failed for %s", slot.key, exc_info=True)
        finally:
            if getattr(slot, "_auto_resume_task", None) is task:
                slot._auto_resume_task = None

    task = asyncio.create_task(_run(), name=f"restart_resume.error:{slot.key}")
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
    loaded: a crewmate thread is measured against the mode its profile declares.
    Run by the boot task (:func:`start_boot_pass`), before the resume pass and
    after the gateway is listening.
    """
    changed = [
        (slot, recorded)
        for slot in list(state._slots.values())
        if (recorded := _recorded_grant_not_held(slot, cfg)) and _eligible_slot(slot)
    ]
    told = 0
    for slot, recorded in changed:
        try:
            await load_window_off_loop(slot)
            if state._slots.get(slot.key) is not slot:
                continue
            if approval_mode_dropped_notice(slot, recorded=recorded):
                told += 1
        except Exception:
            logger.debug(
                "restart_resume: approval-mode notice failed for %s", slot.key, exc_info=True
            )
    return told


def _recorded_grant_not_held(slot: Any, cfg: KiroCrewConfig | None) -> str:
    """The recorded mode when it was a grant the slot does not hold now, else ``""``.

    Nothing to say when the record is absent or ``normal``; when the mode came
    back (crash-only trust once it lands); for an app-owned chat, whose app owns
    the grant and re-grants it (the passes never resume such a chat either); for
    a crewmate thread whose recorded mode is the one its profile declares, which
    ``_seed_member_approval`` re-applies when the thread is opened -- UNLESS the
    boot pass may resume the thread first (the switch is on and the restart cut
    its turn), because that turn would run under the mode the thread holds now,
    which is not the one the person last saw; a thread whose grant was not the
    declared one (an approval card's "trust this session") is always told; or
    when the process-global override is live, because under
    ``agent.dangerously_skip_permissions`` nothing asks and "now on Ask every
    time" would be false.
    """
    recorded = getattr(slot, "_approval_mode_recorded", "") or ""
    if recorded in ("", "normal") or live_approval_mode(slot) == recorded:
        return ""
    if getattr(slot, "_app", ""):
        return ""
    if (
        getattr(slot, "mode", "") == DM_SLOT_MODE
        and _declared_member_mode(cfg, slot) == recorded
        and not (getattr(slot, "_restart_cut_turn", False) and _settings(cfg)[0])
    ):
        return ""
    # Imported here: the override module reads config state this module must not
    # pull in at import time (the codec imports this module on the restore path).
    from kiro_crew.safety_override import safety_override

    if safety_override().is_active():
        return ""
    return recorded


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
    slot: _ChatSlot, *, recorded: str | None = None, cfg: KiroCrewConfig | None = None
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
    """
    if recorded is None:
        recorded = _recorded_grant_not_held(slot, cfg)
    if not recorded:
        return False
    current = live_approval_mode(slot)
    if current == recorded or _newest_status_row_is(slot, APPROVAL_MODE_DROPPED_KIND):
        return False
    remedy = (
        "Opening it from the Crewmates page re-applies the mode its profile "
        "declares; set that mode in the profile to keep it across restarts."
        if getattr(slot, "mode", "") == DM_SLOT_MODE
        else "Pick it again from the composer if you still want it."
    )
    text = (
        "This chat's permissions changed when the app restarted: it was on "
        f"{_APPROVAL_MODE_LABELS.get(recorded, recorded)} and is now on "
        f"{_APPROVAL_MODE_LABELS.get(current, current)}. {remedy}"
    )
    slot.append(
        "assistant",
        text,
        "msg msg-a",
        broadcast=False,
        meta={"kind": APPROVAL_MODE_DROPPED_KIND, "from": recorded, "to": current},
    )
    slot._dirty = True
    _audit(slot, "restart_resume:approval_mode_dropped", f"{recorded}->{current}")
    return True
