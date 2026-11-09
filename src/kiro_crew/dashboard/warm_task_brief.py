"""Return-triggered warm task briefs for active dashboard work.

An owner ``slot_focused`` frame naming a crewmate thread or a Crew Mode root
(:func:`is_brief_thread`) that no owner view already shows is a possible return.  Away time is measured from the latest of:

* when this process last saw the thread on screen in any owner view (a frame
  naming it, a frame or socket close leaving it);
* the owner's last genuine message in the thread's durable history;
* the last brief claim in that history.

The in-process time is what stops a thread watched for hours from being
"welcomed back".  The durable times cover a gateway restart, when the process
has no on-screen record.  A connect announce (first connect or reconnect) for a
thread with no on-screen record is taken as seen now, so the reconnect wave
after a restart is never a return.  A qualifying return claims the durable task
fingerprint before it surfaces status or reads GitHub.

The welcome itself is the gateway's (``warm_task_brief_welcome``, RFC section
3.11): no model writes any of it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from kiro_crew import crew_mode
from kiro_crew import members as members_mod
from kiro_crew.dashboard import warm_task_brief_facts as facts
from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.dashboard import warm_task_brief_welcome as welcome
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.slot_preparing import preparing_message
from kiro_crew.dashboard.slot_retention import _MAX_FOREGROUND_KEY
from kiro_crew.dashboard.warm_task_brief_check import _scan_views, redact_whole
from kiro_crew.dashboard.warm_task_brief_checking import checking_text
from kiro_crew.dashboard.warm_task_brief_inputs import language_of as _language_of
from kiro_crew.dashboard.warm_task_brief_inputs import parse_epoch as _parse_epoch
from kiro_crew.dashboard.warm_task_brief_inputs import redact as _redact
from kiro_crew.dashboard.warm_task_brief_inputs import row_epoch as _row_epoch
from kiro_crew.history import TranscriptBusy, TranscriptWithheld, is_incognito_transcript
from kiro_crew.session_ledger import TERMINAL_PHASES, read_state
from kiro_crew.session_summary import (
    MAX_USER_CHARS,
    TranscriptTurn,
    extract_turns,
    is_injected_text,
)
from kiro_crew.warm_task_brief_contract import WARM_TASK_BRIEF_CLAIM_KIND as _CLAIM_KIND
from kiro_crew.warm_task_brief_contract import WARM_TASK_BRIEF_MESSAGE_KINDS as _MESSAGE_KINDS
from kiro_crew.warm_task_brief_contract import WARM_TASK_BRIEF_RESULT_KIND as _RESULT_KIND

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

WARM_TASK_BRIEF_AWAY_SECS = 30 * 60
# A return while a turn runs waits for the turn to end, at most this long, and
# only while the owner keeps the thread on screen.
WARM_TASK_BRIEF_DEFER_MAX_SECS = 30 * 60
_IDLE_POLL_SECS = 2.0
# Bound on the per-thread "not eligible before" cache. It is only a cache: an
# evicted entry costs one extra history read on the next focus frame.
_MAX_NOT_BEFORE = 256
# Bound on the per-thread "last on screen" record. An evicted thread falls back
# to its durable times, exactly as after a restart.
_MAX_LAST_SEEN = 1024
# Renders one return may make: the first, and up to two redos when a re-read
# renders different text. After that the newest render posts, so a thread that
# changes on every read still gets a welcome.
_MAX_RENDERS = 3
# The clock a digest render reads, so time passing alone never changes it.
_DIGEST_EPOCH = 0.0
# The quiet line is fixed text, never model output, picked by the script of
# the owner's last three genuine messages; Latin-script and unknown text get
# English. It is the whole welcome when nothing changed. It says only "nothing
# new": a question asked in prose before the owner left is still the message
# right above it.
_QUIET_TEXT = {
    "en": "Nothing new since {at}.",
    "zh": "从 {at} 到现在没有新情况。",
    "ja": "{at} から新しいことはありません。",
    "ko": "{at} 이후 새 소식이 없어요.",
    "ru": "С {at} ничего нового.",
    "hi": "{at} से कुछ नया नहीं है।",
    "bn": "{at} থেকে নতুন কিছু নেই।",
}


@dataclass(frozen=True)
class TaskSnapshot:
    slot_key: str
    history_key: str
    original_request: str
    fingerprint: str
    session_state: dict[str, Any]
    conductor: dict[str, Any] | None
    work_items: tuple[dict[str, Any], ...]
    previous_claim: str
    # Digest of the stored ledgers now, and the one the previous brief's
    # claim stored ("" when there was no earlier brief).
    ledger_digest: str = ""
    previous_digest: str = ""
    previous_facts: str = ""  # the newest delivered brief's facts.facts_digest
    # Worker sessions this thread created or revived that are running now;
    # not part of ``fingerprint``.
    running_workers: int = 0
    # Covers only what the person typed. Patrol turns, worker reports and
    # ledger writes change ``fingerprint`` but never make a prepared brief stale.
    user_fingerprint: str = ""
    # Server time of the newest genuine user turn, if it has one.
    last_user_at: float | None = None
    # Server time of the newest brief claim row, if any.
    last_claim_at: float | None = None
    # ``(accepted, total)`` over the current goal's work items, counted by the
    # gateway; ``None`` when there is no count it can stand behind.
    progress: tuple[int, int] | None = None
    # Every turn, whole and in order; the prompt reads the ones since the
    # owner's last genuine message.
    turns: tuple[TranscriptTurn, ...] = ()
    # What waits on the owner now: the "Waiting on you" list
    # (``member_waiting.waiting_items``), newest first.
    waiting: tuple[dict[str, Any], ...] = ()
    # The ``[OPTIONS:]`` line the owner was left with, copied onto the brief.
    pending_options: str = ""

    def away_anchor(self, seen_at: float | None = None) -> float | None:
        """When the owner was last known present: the latest of *seen_at*
        (last on screen in this process), their last genuine message and the
        last brief. ``None`` when none has a time."""
        times = [t for t in (seen_at, self.last_user_at, self.last_claim_at) if t is not None]
        return max(times) if times else None


def cancel_warm_task_brief(state: "DashboardState", slot_key: str) -> None:
    """Cancel one in-flight welcome; the task's own exit drops its indicator."""
    tasks = getattr(state, "_warm_task_brief_tasks", None)
    task = tasks.get(slot_key) if isinstance(tasks, dict) else None
    current = asyncio.current_task() if task is not None else None
    if task is not None and task is not current and not task.done():
        task.cancel()
        _skip(slot_key, "cancelled")
    if isinstance(tasks, dict) and (task is None or task is not current):
        tasks.pop(slot_key, None)


def cancel_for_live_user_row(state: "DashboardState", slot_key: str, content: object) -> None:
    """Cancel on a row the person typed; automation rows like a wake never do."""
    if isinstance(content, str) and not is_injected_text(content):
        cancel_warm_task_brief(state, slot_key)


# Skip reasons that explain why a real return to a crewmate thread got no brief.
# They log at INFO the first time a thread hits each one in a row; the reasons
# every ordinary focus frame produces (on_screen, short_away, ...) stay DEBUG.
_INFO_SKIPS = frozenset({"no_open_task", "no_request", "no_user_time", "snapshot_unreadable"})
# Per thread, the INFO reason last logged. Bounded like the on-screen record.
_skips_logged: dict[str, str] = {}


def _skip(slot_key: str, reason: str) -> None:
    """One log line per skipped brief: the thread key and a fixed reason."""
    if reason in _INFO_SKIPS and _skips_logged.get(slot_key) != reason:
        _skips_logged.pop(slot_key, None)
        _skips_logged[slot_key] = reason
        while len(_skips_logged) > _MAX_LAST_SEEN:
            _skips_logged.pop(next(iter(_skips_logged)))
        logger.info("warm task brief skipped for %s: %s", slot_key, reason)
        return
    logger.debug("warm task brief skipped for %s: %s", slot_key, reason)


def _remember_not_before(state: "DashboardState", slot_key: str, instant: float) -> None:
    """Cache that *slot_key* cannot qualify before *instant*.

    Safe because the away anchor only moves later: a new message or claim
    pushes it forward, never back.  The cache only saves history reads.
    """
    cache = getattr(state, "_warm_task_brief_not_before", None)
    if not isinstance(cache, dict):
        return
    cache.pop(slot_key, None)
    cache[slot_key] = instant
    while len(cache) > _MAX_NOT_BEFORE:
        cache.pop(next(iter(cache)))


def _stamp_seen(state: "DashboardState", slot_key: str, instant: float) -> None:
    """Record that *slot_key* was on screen in an owner view at *instant*."""
    seen = getattr(state, "_warm_task_brief_last_seen", None)
    if not isinstance(seen, dict):
        return
    seen.pop(slot_key, None)
    seen[slot_key] = instant
    while len(seen) > _MAX_LAST_SEEN:
        seen.pop(next(iter(seen)))


def _shown_by_other(foreground: object, conn: int, slot_key: str) -> bool:
    return isinstance(foreground, dict) and any(
        key == slot_key for other, key in foreground.items() if other != conn
    )


def is_brief_thread(slot: "_ChatSlot") -> bool:
    """Whether *slot* may get a return brief: a crewmate thread or a Crew Mode root.

    A Crew Mode root is a chat the owner switched onto a conductor
    (:func:`kiro_crew.crew_mode.slot_is_crew_mode_root`); its workers are not.
    """
    return slot.mode == members_mod.DM_SLOT_MODE or crew_mode.slot_is_crew_mode_root(slot)


def note_view_closed(state: "DashboardState", conn: int) -> None:
    """An owner socket closed: the thread it showed left that view's screen.

    Runs before ``slot_retention.note_foreground`` clears the row.
    """
    foreground = getattr(state, "_foreground_slots", None)
    previous = foreground.get(conn) if isinstance(foreground, dict) else None
    if isinstance(previous, str) and previous:
        _stamp_seen(state, previous, time.time())


def handle_focus_frame(
    state: "DashboardState",
    conn: int,
    slot_key: object,
    *,
    announce: bool = False,
) -> None:
    """Handle one owner focus frame; schedule at most one brief for its thread.

    Runs before ``slot_retention.note_foreground`` applies the frame, because
    it reads the connection's previous thread. *announce* marks the frame a
    socket sends when it connects; it is never a return for a thread this
    process holds no on-screen record of. Two windows on one thread are one
    view: a frame for a thread another owner view shows is not a return.

    A crewmate thread and a Crew Mode root share one leave rule: a switch to
    another chat (sidebar or another split-view pane) leaves the thread, as a
    page hide does, because the owner's attention left it.
    """
    now = time.time()
    foreground = getattr(state, "_foreground_slots", None)
    previous = foreground.get(conn) if isinstance(foreground, dict) else None
    if isinstance(previous, str) and previous and slot_key != previous:
        _stamp_seen(state, previous, now)
        # The last owner view leaving a thread drops a brief whose "preparing"
        # indicator has not yet shown. Once it shows, leaving is only a pause:
        # the result still lands as ordinary history. Another window still
        # showing the thread keeps the shared brief alive either way.
        opened = getattr(state, "_warm_task_brief_opened", ())
        if previous not in opened and not _shown_by_other(foreground, conn, previous):
            cancel_warm_task_brief(state, previous)
    # The foreground registry's key bound, before any key is retained.
    if not isinstance(slot_key, str) or not slot_key or len(slot_key) > _MAX_FOREGROUND_KEY:
        return
    seen = getattr(state, "_warm_task_brief_last_seen", None)
    seen_at = seen.get(slot_key) if isinstance(seen, dict) else None
    _stamp_seen(state, slot_key, now)
    if previous == slot_key or _shown_by_other(foreground, conn, slot_key):
        _skip(slot_key, "on_screen")
        return
    if seen_at is None and announce:
        _skip(slot_key, "connect_announce")
        return
    if seen_at is not None and now - seen_at < WARM_TASK_BRIEF_AWAY_SECS:
        _skip(slot_key, "short_away")
        return
    not_before = getattr(state, "_warm_task_brief_not_before", {}).get(slot_key)
    if isinstance(not_before, float) and now < not_before:
        _skip(slot_key, "short_away")
        return
    slot = state.get_slot(slot_key)
    if slot is None or not is_brief_thread(slot):
        _skip(slot_key, "not_member_thread")
        return
    tasks = state._warm_task_brief_tasks
    existing = tasks.get(slot_key)
    if existing is not None and not existing.done():
        _skip(slot_key, "already_preparing")
        return
    task = asyncio.create_task(_prepare_warm_task_brief(state, slot, seen_at))
    tasks[slot_key] = task

    def _retire(done: asyncio.Task[Any]) -> None:
        if tasks.get(slot_key) is done:
            tasks.pop(slot_key, None)

    task.add_done_callback(_retire)


def _bounded_request(text: str) -> str:
    if len(text) <= MAX_USER_CHARS:
        return text
    # The marker's length depends on the count it names, so settle both together.
    keep = MAX_USER_CHARS
    while True:
        marker = f"\n[... {len(text) - keep} characters omitted ...]"
        fitted = max(0, MAX_USER_CHARS - len(marker))
        if fitted == keep:
            return text[:keep] + marker
        keep = fitted


def _record_claim(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        meta = row.get("meta")
        if (
            isinstance(meta, dict)
            and meta.get("kind") == _CLAIM_KIND
            and isinstance(meta.get("fingerprint"), str)
        ):
            return meta["fingerprint"]
    return ""


# Set on a result row an older, model-written brief posted after a section
# 3.9.6 grounding miss. A welcome never writes it; a stored one still means
# that row caught the owner up on nothing.
_GROUNDING_MISS_KEY = "grounding_miss"


def _claim_digest(rows: list[dict[str, Any]]) -> str:
    """The ledger digest the newest claim row stored, or "".

    It counts only when a delivered result row followed that claim: a brief
    that failed, was dropped, or fell back to the gateway's own cards after a
    grounding miss caught the owner up on nothing, so there is no digest to
    match and the next return writes the full welcome."""
    answered = False
    for row in reversed(rows):
        meta = row.get("meta")
        if not isinstance(meta, dict):
            continue
        if meta.get("kind") == _RESULT_KIND and not meta.get(_GROUNDING_MISS_KEY):
            answered = True
        elif meta.get("kind") == _CLAIM_KIND:
            digest = meta.get("ledger_digest")
            return digest if answered and isinstance(digest, str) else ""
    return ""


def _claim_epoch(rows: list[dict[str, Any]]) -> float | None:
    """Server time of the newest claim row; a brief counts as presence."""
    for row in reversed(rows):
        meta = row.get("meta")
        if isinstance(meta, dict) and meta.get("kind") == _CLAIM_KIND:
            epoch = _row_epoch(row.get("ts"))
            if epoch is not None:
                return epoch
    return None


def task_records(slot_key: str) -> tuple[Any, list[Any]]:
    """Read work records through the dashboard's existing identity seam."""
    from kiro_crew.dashboard.handlers.work_ledger_board import task_records as read_records

    return read_records(slot_key)


def work_item_is_terminal(state: object) -> bool:
    """Read terminal-state policy without loading dashboard handlers at boot."""
    from kiro_crew.dashboard.handlers.work_ledger_board import work_item_is_terminal as check

    return check(state)


def _event_heads(slot_key: str, items: list[Any]) -> dict[str, str]:
    """Each open item's newest event id, from the tail the welcome's
    ``awaits_person`` read uses. A tail that cannot be read counts as none.
    FILE I/O."""
    from kiro_crew.dashboard.handlers.work_ledger_board import _tail_events

    heads: dict[str, str] = {}
    for item in items:
        if getattr(item, "is_terminal", True):
            continue
        try:
            tail = _tail_events(slot_key, str(item.item_id), 1)
        except Exception:
            logger.debug("no event tail for a warm welcome digest", exc_info=True)
            continue
        if tail:
            heads[str(item.item_id)] = tail[-1].id
    return heads


def _work_item_payload(item: Any) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "title": item.title,
        "pr": getattr(item, "pr", None),
        # The live read takes a PR's repo from here: ledger data, never text.
        "acceptance": getattr(item, "acceptance", None) or {},
        "state": item.state,
        "verdict": item.verdict,
        "decision": item.decision,
        "worker_session_key": item.worker_session_key,
        "status": item.status,
        "reason": item.reason,
        "summary": item.summary,
        "artifacts": item.artifacts,
        "last_report_at": item.last_report_at,
        "created_at": item.created_at,
        "closed_at": item.closed_at,
        "decided_at": item.decided_at,
        # A remote worker's binding: the Board's liveness readers need it.
        "worker_instance_id": getattr(item, "worker_instance_id", None),
        # What the item needs from the person, as the conductor recorded it.
        "ask": getattr(item, "ask", None),
    }


def _conductor_payload(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "slot_key": record.slot_key,
        "goal": record.goal,
        "round": record.round,
        "depth": record.depth,
        "parent_item": record.parent_item,
        "generation": record.generation,
        "goal_version": record.goal_version,
        "created_at": record.created_at,
        "created_total": record.created_total,
        "concurrency_cap": record.concurrency_cap,
    }


def _goal_progress(record: Any, items: tuple[dict[str, Any], ...]) -> tuple[int, int] | None:
    """``(accepted, total)`` over the current goal's work items, or ``None``.

    A board keeps every goal's items, so after a ``new_goal`` write only the
    newest ``goal_items_used`` items belong to the goal. When that subset cannot
    be named (an unreadable header, a header from before the per-goal count, a
    stored-item count that disagrees with the header, or creation times that
    cannot order the cut) there is no count rather than a wrong one. A board
    that never recorded a ``new_goal`` (``goal_version == 0``) has no count
    either: its items span every goal the thread ever had, so "142 of 170"
    would read as progress on today's work when it is not. A session-ledger
    phase alone has no denominator.
    """
    if record is None or getattr(record, "goal_version", 0) <= 0:
        return None
    used = getattr(record, "goal_items_used", 0)
    # Header and items are separate reads: a mismatch cannot name the subset.
    if getattr(record, "goal_items_base", -1) < 0 or len(items) != record.created_total:
        return None
    if used < len(items):
        epochs = [_parse_epoch(item.get("created_at")) for item in items]
        if any(epoch is None for epoch in epochs):
            return None
        order = sorted(range(len(items)), key=lambda i: epochs[i] or 0.0)
        cut = len(order) - used
        # Creation times are whole seconds: a tie at the cut hides the order.
        if cut > 0 and used and epochs[order[cut - 1]] == epochs[order[cut]]:
            return None
        items = tuple(items[i] for i in order[cut:])
    if not items:
        return None
    return sum(1 for item in items if item.get("state") == "accepted"), len(items)


def _has_open_task(
    session_state: dict[str, Any],
    conductor: dict[str, Any] | None,
    items: tuple[dict[str, Any], ...],
    *,
    running_workers: int = 0,
) -> bool:
    # A crewmate that drives its work through session_create keeps no ledger
    # record, so its running worker sessions are the in-flight signal.
    if running_workers > 0:
        return True
    if any(not work_item_is_terminal(item.get("state")) for item in items):
        return True
    phase = str(session_state.get("phase") or "").strip().lower()
    if phase and phase not in TERMINAL_PHASES:
        return True
    return bool(conductor and conductor.get("goal") and not items)


def _original_request_for_task(
    turns: list[TranscriptTurn],
    session_state: dict[str, Any],
    conductor: dict[str, Any] | None,
    items: tuple[dict[str, Any], ...],
) -> str:
    """Pick the genuine request immediately preceding this task's durable start."""
    user_turns = [
        turn for turn in turns if turn.role == "user" and not turn.injected and turn.text.strip()
    ]
    if not user_turns:
        return ""
    anchors = [
        epoch
        for item in items
        if not work_item_is_terminal(item.get("state"))
        for epoch in [_parse_epoch(item.get("created_at"))]
        if epoch is not None
    ]
    if not anchors:
        conductor_epoch = _parse_epoch((conductor or {}).get("created_at"))
        if conductor_epoch is not None:
            anchors.append(conductor_epoch)
    if not anchors:
        session_epoch = _parse_epoch(session_state.get("created_at"))
        if session_epoch is not None:
            anchors.append(session_epoch)
    if anchors:
        task_start = min(anchors)
        preceding = [
            turn
            for turn in user_turns
            if (turn_epoch := _parse_epoch(turn.ts)) is not None and turn_epoch <= task_start
        ]
        if preceding:
            return _bounded_request(preceding[-1].text)
    return _bounded_request(user_turns[-1].text)


def _running_workers(state: "DashboardState", slot_key: str) -> int:
    """Worker slots *slot_key* created or revived that are running now.

    A finished worker whose tab stays open is not in-flight work, so this
    counts only slots holding a turn reservation. In memory, loop thread only.
    """
    return sum(
        1
        for slot in state._slots.values()
        if slot.running and slot_key in (slot._created_by, slot._revived_by)
    )


def _snapshot_sync(
    state: "DashboardState",
    slot: "_ChatSlot",
    running_workers: int = 0,
    why: list[str] | None = None,
    *,
    require_open: bool = True,
) -> TaskSnapshot | None:
    """The task snapshot, or ``None`` with the skip reason appended to *why*.
    Its ``waiting`` list is left empty; ``_snapshot`` reads it last, on the loop.

    *require_open* applies the open-work gate. Only the first read decides
    eligibility; a re-read passes ``False`` so work closing mid-brief does not
    drop a brief already underway.
    """

    def none(reason: str) -> TaskSnapshot | None:
        if why is not None:
            why.append(reason)
        return None

    from kiro_crew.dashboard import member_waiting

    history_key = slot_history_key(slot)
    log = state.conversation_log
    if log is None:
        return none("snapshot_unreadable")
    # Every focus frame on a crewmate thread can land here, so the small
    # ledger reads gate the full history read.
    session_state = read_state(slot.key)
    conductor_record, items_raw = task_records(slot.key)
    conductor = _conductor_payload(conductor_record)
    items = tuple(_work_item_payload(item) for item in items_raw)
    if require_open and not _has_open_task(
        session_state, conductor, items, running_workers=running_workers
    ):
        return none("no_open_task")
    rows = log.derive_messages_chained(history_key)
    task_rows_source = [
        row
        for row in rows
        if not (
            isinstance(row.get("meta"), dict)
            and isinstance(row["meta"].get("kind"), str)
            and row["meta"]["kind"] in _MESSAGE_KINDS
        )
    ]
    # Keep every turn whole here: the prompt's transcript section budgets whole
    # turns, and _bounded_request owns the exact 4,000 character quote.
    turns = extract_turns(task_rows_source, assistant_excerpt_chars=2**62, max_user_chars=2**63 - 1)
    original = _original_request_for_task(turns, session_state, conductor, items)
    if not original:
        return none("no_request")

    task_rows: list[dict[str, str]] = []
    for turn in turns:
        if turn.role == "user" and turn.injected:
            continue
        task_rows.append(
            {
                "role": turn.role,
                "content_sha256": hashlib.sha256(turn.text.encode("utf-8")).hexdigest(),
            }
        )

    fingerprint_source = {
        "original_request": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "task_rows": task_rows,
        "session_state": session_state,
        "conductor": conductor,
        "work_items": items,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    user_fingerprint = hashlib.sha256(
        json.dumps([row["content_sha256"] for row in task_rows if row["role"] == "user"]).encode(
            "utf-8"
        )
    ).hexdigest()
    return TaskSnapshot(
        slot_key=slot.key,
        history_key=history_key,
        original_request=original,
        fingerprint=fingerprint,
        session_state=session_state,
        conductor=conductor,
        work_items=items,
        running_workers=running_workers,
        progress=_goal_progress(conductor_record, items),
        turns=tuple(turns),
        previous_claim=_record_claim(rows),
        pending_options=member_waiting.pending_options(rows) or "",
        ledger_digest=inputs.ledger_digest(
            conductor_record, items_raw, session_state, _event_heads(slot.key, items_raw)
        ),
        previous_digest=_claim_digest(rows),
        previous_facts=facts.previous_facts_digest(rows, _RESULT_KIND, _GROUNDING_MISS_KEY),
        last_claim_at=_claim_epoch(rows),
        user_fingerprint=user_fingerprint,
        last_user_at=max(
            (
                epoch
                for turn in turns
                if turn.role == "user" and not turn.injected
                for epoch in [_row_epoch(turn.ts)]
                if epoch is not None
            ),
            default=None,
        ),
    )


async def _warm_cards(state: "DashboardState", slot_key: str) -> bool:
    """Warm the change-card store off the loop, as ``crewmate_feed`` does.

    ``False`` when the store cannot be read: the "Waiting on you" list then
    leaves the cards out.
    """
    from kiro_crew.dashboard import change_cards

    try:
        await change_cards.card_store_for(state).warm()
    except Exception:
        logger.debug("warm task brief: card store unreadable for %s", slot_key, exc_info=True)
        return False
    return True


def _waiting(state: "DashboardState", slot_key: str, *, cards: bool) -> tuple[dict[str, Any], ...]:
    """The thread's "Waiting on you" list, read on the loop with no suspension,
    so a card answered or declined before this call is never listed."""
    from kiro_crew.dashboard import member_waiting

    return tuple(member_waiting.waiting_items(state, slot_key, cards=cards))


async def _snapshot(
    state: "DashboardState",
    slot: "_ChatSlot",
    why: list[str] | None = None,
    *,
    require_open: bool = True,
) -> TaskSnapshot | None:
    """The task snapshot, or ``None`` with the skip reason appended to *why*."""
    try:
        # The slot registry is read here, on the loop, never from the worker thread.
        running_workers = _running_workers(state, slot.key)
        cards = await _warm_cards(state, slot.key)
        snapshot = await asyncio.to_thread(
            _snapshot_sync,
            state,
            slot,
            running_workers,
            why,
            require_open=require_open,
        )
        if snapshot is None:
            return None
        # The waiting list is read last, after every suspension: the callers
        # compare and post with no await after this returns, so what posts
        # is the list as it stood at publication.
        return replace(snapshot, waiting=_waiting(state, slot.key, cards=cards))
    except (TranscriptBusy, TranscriptWithheld):
        pass
    except Exception:
        logger.debug("warm task brief snapshot failed for %s", slot.key, exc_info=True)
    if why is not None:
        why.append("snapshot_unreadable")
    return None


def _same_slot(state: "DashboardState", slot: "_ChatSlot") -> bool:
    return state.get_slot(slot.key) is slot and not is_incognito_transcript(
        getattr(slot, "memory_mode", "")
    )


def _is_foreground(state: "DashboardState", slot_key: str) -> bool:
    foreground = getattr(state, "_foreground_slots", None)
    return isinstance(foreground, dict) and slot_key in foreground.values()


async def _still_current(
    state: "DashboardState",
    slot: "_ChatSlot",
    expected: TaskSnapshot,
    *,
    require_claim: bool = False,
    require_foreground: bool = True,
) -> TaskSnapshot | None:
    if (
        not _same_slot(state, slot)
        or slot.running
        or (require_foreground and not _is_foreground(state, slot.key))
    ):
        return None
    # Eligibility was settled by the first read. Open work closing since then
    # (the last worker stopping, an item turning terminal) is not staleness.
    current = await _snapshot(state, slot, require_open=False)
    if (
        current is None
        or current.user_fingerprint != expected.user_fingerprint
        or (require_claim and current.previous_claim != expected.fingerprint)
    ):
        return None
    return current


async def _wait_for_idle_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    deadline: float,
    *,
    require_foreground: bool = True,
) -> bool:
    """Wait until *slot* runs no turn, while the owner keeps it on screen when
    *require_foreground* holds."""
    while slot.running:
        if (
            not _same_slot(state, slot)
            or (require_foreground and not _is_foreground(state, slot.key))
            or time.monotonic() >= deadline
        ):
            return False
        await asyncio.sleep(_IDLE_POLL_SECS)
    return True


async def _settled(
    state: "DashboardState",
    slot: "_ChatSlot",
    expected: TaskSnapshot,
    deadline: float,
    *,
    require_foreground: bool = True,
) -> TaskSnapshot | None:
    """Re-read the claimed task once no turn runs; ``None`` means stale."""
    while True:
        if not await _wait_for_idle_turn(
            state, slot, deadline, require_foreground=require_foreground
        ):
            _skip(slot.key, "turn_outlasted_return")
            return None
        # A send's row may still sit in memory; the re-read below reads disk.
        await asyncio.to_thread(state.flush_slot_now, slot)
        current = await _still_current(
            state, slot, expected, require_claim=True, require_foreground=require_foreground
        )
        if current is not None or not slot.running:
            if current is None:
                _skip(slot.key, "stale")
            return current


def _goal(snapshot: TaskSnapshot) -> str:
    """What the thread is for now: the conductor goal, the session ledger's
    goal, else the owner's newest genuine message."""
    for value in ((snapshot.conductor or {}).get("goal"), snapshot.session_state.get("goal")):
        if isinstance(value, str) and value.strip():
            return _bounded_request(redact_whole(value.strip()))
    for turn in reversed(snapshot.turns):
        if turn.role == "user" and not turn.injected and turn.text.strip():
            # Redacted whole before the bound cuts it.
            return _bounded_request(redact_whole(turn.text))
    return snapshot.original_request


def _ask_items(snapshot: TaskSnapshot) -> list[dict[str, Any]]:
    """Open work items waiting on a question or a block."""
    return [item for item in snapshot.work_items if inputs.is_open_ask(item, work_item_is_terminal)]


def _live_refs(snapshot: TaskSnapshot) -> list[inputs.PrRef]:
    """The PRs read before the brief may name them (``facts.live_refs``)."""
    return facts.live_refs(snapshot.work_items, work_item_is_terminal)


def _is_quiet(snapshot: TaskSnapshot, previous_digest: str, anchor: float | None) -> bool:
    """Nothing changed since the previous welcome and nothing waits on the owner.

    "Changed" is any stored write to the work ledger or session ledger, read
    as a digest the previous welcome's claim kept. No earlier welcome means no
    digest to match, so the full welcome is written.
    """
    return (
        bool(previous_digest)
        and previous_digest == snapshot.ledger_digest
        and not snapshot.waiting
        and not _ask_items(snapshot)
        and not inputs.assistant_spoke_since(snapshot.turns, anchor)
    )


# A chat control marker the dashboard acts on when it reads an assistant row's
# ``content``: ``[OPTIONS: …]`` becomes send buttons, ``[STEERING …]`` a chip,
# and an HTML comment such as ``<!-- kirocrew:open-file … -->`` a file chip or a
# transcript control. Every opener spelling the option parser accepts is caught
# (ASCII and the three full-width brackets, any case).
_CONTROL_MARKER_RE = re.compile(
    r"[\[\u3010\uFF3B\u3014](?=\s*(?:OPTIONS?|STEERING)\b)", re.IGNORECASE
)

_HTML_COMMENT_OPENER_RE = re.compile(r"<!-+")


def _defang_markers(text: str) -> str:
    """Make a ledger string unable to act as a chat control marker.

    The welcome's ``content`` is read by the option bar and other marker readers
    on every surface, so a title or a reason copied from a transcript would
    otherwise turn into live controls; the welcome's only live marker is the
    gateway-copied ``[OPTIONS:]`` line. The marker stays readable.
    """
    # The whole dash run collapses to one, so the result can never re-form an
    # opener: ``<!---`` must not become ``<!--`` after one dash is removed.
    return _HTML_COMMENT_OPENER_RE.sub("<!-", _CONTROL_MARKER_RE.sub("(", text))


def _holds_a_secret(text: str) -> bool:
    """Whether the redactor finds anything in any of *text*'s scan views."""
    return any(_redact(view) != view for view in _scan_views(text))


# Every span the chat's GFM autolinker would turn into a link: a web address
# (protocol or ``www.``) and an email address. It is matched anywhere, a superset
# of what GFM links, so none is left as text for the autolinker to find.
_ADDRESS_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>()`]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE
)
# Every character the chat's markdown can read as markup. CommonMark lets each be
# backslash-escaped, and an escaped one always renders as itself. ``(`` stays as
# written: it is markup only after a live ``]``, none is left, and ``\(`` would
# open the chat's math. ``?``, ``,``, ``:`` and the like are never markup.
_MARKDOWN_PUNCT_RE = re.compile(r"[\\`*_\[\]<>!#+\-.|~&$=)]")
# The chat renders ``\(…\)`` and ``\[…\]`` as math, so a backslash or a bracket is
# written as a character reference: no backslash can ever precede ``(`` or ``[``.
# Angle brackets are references too: the chat strips everything after a literal
# ``<mcwidget`` or ``<tool_use`` in the raw text, escaped or not.
_AS_REFERENCE = {"\\": "&#92;", "[": "&#91;", "]": "&#93;", "<": "&#60;", ">": "&#62;"}


def _escape_punct(match: re.Match[str]) -> str:
    char = match.group(0)
    return _AS_REFERENCE.get(char) or "\\" + char


def _inert_markdown(text: str) -> str:
    """*text* as markdown that renders back to exactly *text*, with nothing live.

    Outside an address every punctuation character is escaped, so no link,
    image, emphasis, math, HTML tag, entity or code span can form; each address
    is a code span, which the autolinker leaves alone and which shows it as
    written.
    """
    out: list[str] = []
    at = 0
    for match in _ADDRESS_RE.finditer(text):
        out.append(_MARKDOWN_PUNCT_RE.sub(_escape_punct, text[at : match.start()]))
        out.append(f"`{match.group(0)}`")
        at = match.end()
    out.append(_MARKDOWN_PUNCT_RE.sub(_escape_punct, text[at:]))
    return "".join(out)


def _live_options(slot: "_ChatSlot", settled: TaskSnapshot) -> str:
    """The choices to copy at post time, for every brief post.

    The slot's in-memory window wins when it decides: a note appended after
    the last flush is in memory before it is on disk.
    """
    from kiro_crew.dashboard import member_waiting

    rows = list(getattr(slot, "messages", None) or [])
    in_memory = member_waiting.pending_options(rows)
    return settled.pending_options if in_memory is None else in_memory


def _clean(value: object, cap: int) -> str:
    """One redacted plain-text line for the welcome's markdown: whitespace
    collapsed, bold markers dropped, control markers defanged, capped, then
    escaped so it renders back as written with nothing live
    (:func:`_inert_markdown`).

    Every scan view of the WHOLE value is redacted before anything cuts it
    (:func:`redact_whole`): a cut first could split a credential into a piece
    no redactor recognises. Redaction runs again on the normalized text,
    because dropping ``**`` can rejoin a token the first pass saw split.
    """
    if not isinstance(value, str):
        return ""
    text = _redact(" ".join(_redact(redact_whole(value)).replace("**", "").split()))
    # Cut before the address is wrapped: a cut inside the wrap would drop its
    # closing backtick and leave the address live.
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return _inert_markdown(_defang_markers(text))


def _post_message(
    state: "DashboardState",
    slot: "_ChatSlot",
    content: str,
    *,
    kind: str,
    fingerprint: str,
    options: str = "",
    facts_digest: str = "",
    reply_hint: str = "",
) -> dict[str, Any]:
    """Post one crewmate message; *options* is its one live, gateway-copied marker.
    *reply_hint* goes just above it, so each chip reads as a reply, not an action."""
    if options:
        hint = f"{reply_hint}\n\n" if reply_hint else ""
        content = f"{content}\n\n{hint}{options}"
    from kiro_crew.dashboard.state import append_and_surface

    meta: dict[str, Any] = {"kind": kind, "fingerprint": fingerprint}
    if facts_digest:
        meta[facts.FACTS_DIGEST_KEY] = facts_digest
    return append_and_surface(state, slot, "assistant", content, "msg msg-a", meta=meta)


async def _prepare_warm_task_brief(
    state: "DashboardState", slot: "_ChatSlot", seen_at: float | None = None
) -> None:
    # The preparing indicator is left on every exit: result, stale drop,
    # cancellation or error.
    try:
        with ExitStack() as indicator:
            await _run_warm_task_brief(state, slot, indicator, seen_at)
    finally:
        opened = getattr(state, "_warm_task_brief_opened", None)
        if isinstance(opened, set):
            opened.discard(slot.key)


async def _run_warm_task_brief(
    state: "DashboardState",
    slot: "_ChatSlot",
    indicator: ExitStack,
    seen_at: float | None = None,
) -> None:
    # A return during a turn (a patrol cycle, say) defers until the turn ends,
    # as long as the owner keeps the thread on screen.
    returned_at = time.time()
    deadline = time.monotonic() + WARM_TASK_BRIEF_DEFER_MAX_SECS
    if not await _wait_for_idle_turn(state, slot, deadline):
        _skip(slot.key, "turn_outlasted_return")
        return
    if not _same_slot(state, slot) or slot.running or not _is_foreground(state, slot.key):
        _skip(slot.key, "not_current")
        return
    why: list[str] = []
    snapshot = await _snapshot(state, slot, why)
    if snapshot is None:
        _skip(slot.key, why[0] if why else "snapshot_unreadable")
        return
    if snapshot.previous_claim == snapshot.fingerprint:
        _skip(slot.key, "already_claimed")
        return
    # Away runs from the latest presence this process saw or history holds. A
    # message typed after returning (say, during a deferred turn) is short too.
    anchor = snapshot.away_anchor(seen_at)
    if anchor is None:
        _skip(slot.key, "no_user_time")
        return
    if returned_at - anchor < WARM_TASK_BRIEF_AWAY_SECS:
        _remember_not_before(state, slot.key, anchor + WARM_TASK_BRIEF_AWAY_SECS)
        _skip(slot.key, "short_away")
        return

    # A display-only notice cannot enter model replay. It is deliberately not
    # broadcast; its renderer suppresses it on reload as derived state. The
    # claim must reach disk before any GitHub read: the re-read below requires it.
    slot.append(
        "notice",
        "",
        broadcast=False,
        meta={
            "kind": _CLAIM_KIND,
            "fingerprint": snapshot.fingerprint,
            "ledger_digest": snapshot.ledger_digest,
        },
    )
    # _settled flushes the claim before its re-read. A patrol turn admitted
    # since the snapshot defers the indicator, too.
    fresh = await _settled(state, slot, snapshot, deadline)
    if fresh is None:
        return
    # The claim and every staleness check keep the first snapshot's
    # fingerprint; what the welcome SAYS comes from the settled re-read.
    language = _language_of(" ".join(inputs.owner_samples(fresh.turns)) or fresh.original_request)
    quiet = _QUIET_TEXT[language].format(at=inputs.clock(anchor))
    if _is_quiet(fresh, snapshot.previous_digest, anchor):
        # Nothing to catch up on: one fixed line and no GitHub read. It claims
        # no task status (a blocked task stays blocked).
        _post_message(
            state,
            slot,
            quiet,
            kind=_RESULT_KIND,
            fingerprint=snapshot.fingerprint,
            options=_live_options(slot, fresh),
            reply_hint=welcome.WORDS[language]["tap_reply"],
        )
        return
    # The hint goes up before the first GitHub read, inside the reply bubble the
    # welcome replaces. It marks no turn (the deferral reads ``slot.running``).
    # Its caption names what is checked; the goal is redacted whole, before the cut.
    refs = _live_refs(fresh)
    open_items = inputs.open_item_entries(fresh.work_items, anchor, work_item_is_terminal)
    goal = _redact(redact_whole(_goal(fresh)))
    caption = checking_text(language, goal, len(refs), len(open_items), fresh.running_workers)
    indicator.enter_context(preparing_message(state, slot, caption, language))
    # Leaving now keeps the welcome: only a typed row, a reset or a close cancels it.
    opened = getattr(state, "_warm_task_brief_opened", None)
    if isinstance(opened, set):
        opened.add(slot.key)
    read_left = inputs.LIVE_READ_TOTAL_SECS
    unit = _crew_log_unit(state, slot)
    # Once the hint shows, the welcome lands as ordinary history even while the
    # owner is away from the thread. A turn that started meanwhile defers
    # publication until it ends.
    settled = await _settled(state, slot, snapshot, deadline, require_foreground=False)
    if settled is None:
        return
    # One clock for every render of this return: "checked HH:MM" is when the
    # ledger was first read, and a re-render that only the clock would change
    # renders the same text.
    checked_epoch = time.time()
    content = ""
    for _ in range(_MAX_RENDERS):
        started = time.monotonic()
        live = tuple(
            await inputs.read_live_prs(_live_refs(settled), budget=max(read_left, 0.0), unit=unit)
        )
        read_left -= time.monotonic() - started
        content = await _render(state, slot, settled, live, language, checked_epoch)
        # Read again after every await and render what that read says: any
        # input that moved during the reads (a ledger write, an answered card, a
        # worker's turn ending) changes the text.
        current = await _settled(state, slot, snapshot, deadline, require_foreground=False)
        if current is None:
            return
        again = await _compose(state, slot, current, live, language, checked_epoch)
        settled = current
        stable = again == content
        content = again
        if stable:
            break
    # shortcut: past _MAX_RENDERS the newest render posts even if the next read
    # would differ; the next return writes it again.
    digest = facts.facts_digest(
        await _compose(state, slot, settled, facts.timeless(live), language, _DIGEST_EPOCH)
    )
    # No await sits between this check and the post.
    if slot.running or not _same_slot(state, slot):
        _skip(slot.key, "changed_during_write")
        return
    if not content:
        content = quiet  # the final secret scan dropped the welcome
    elif digest == settled.previous_facts and not inputs.assistant_spoke_since(
        settled.turns, anchor
    ):
        # Nothing changed since the last welcome, PR reads included: the quiet line.
        content = quiet
    # The buttons come from the last re-read, as the chip bar would offer them.
    _post_message(
        state,
        slot,
        content,
        kind=_RESULT_KIND,
        fingerprint=snapshot.fingerprint,
        options=_live_options(slot, settled),
        facts_digest=digest,
        reply_hint=welcome.WORDS[language]["tap_reply"],
    )


def _crew_log_unit(state: "DashboardState", slot: "_ChatSlot") -> str:
    """The crew log unit the thread serves on now, where its PR reads are recorded
    for the ``objects`` slot fold; "" when it has none (nothing is recorded)."""
    from kiro_crew.crew_log.resolve import unit_for_session_key

    try:
        return unit_for_session_key(state.sessions, effective_session_key(slot)) or ""
    except Exception:
        logger.debug("warm task brief: no crew log unit for %s", slot.key, exc_info=True)
        return ""


async def _render(
    state: "DashboardState",
    slot: "_ChatSlot",
    settled: TaskSnapshot,
    live: tuple[dict[str, Any], ...],
    language: str,
    checked_epoch: float,
) -> str:
    """One attempt's welcome (:func:`_compose`). The re-read check and the digest
    compose again directly, so one call here is one attempt."""
    return await _compose(state, slot, settled, live, language, checked_epoch)


async def _compose(
    state: "DashboardState",
    slot: "_ChatSlot",
    settled: TaskSnapshot,
    live: tuple[dict[str, Any], ...],
    language: str,
    checked_epoch: float,
) -> str:
    """The welcome's markdown, every fact from the gateway's own reads
    (:mod:`warm_task_brief_welcome`); "waits on you" is the Board's own test."""
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    items = tuple(i for i in settled.work_items if i.get("item_id"))
    open_items = [i for i in items if not work_item_is_terminal(i.get("state"))]
    # Memory reads, on the loop; a remote worker's turn is read off it below.
    local = {str(i["item_id"]): board.item_running(state, i) for i in open_items}

    def reads() -> tuple[dict[str, bool], tuple[dict[str, Any], ...]]:
        awaits = {
            str(i["item_id"]): board.item_awaits_person(
                slot.key, i, running=local[str(i["item_id"])]
            )
            for i in open_items
        }
        # An ask record older than the item's newest report, in the ledger's own
        # event order, explains an earlier question: it is not shown.
        shown = tuple(
            (
                {**i, "ask": None}
                if i.get("ask") and not board.item_ask_is_current(slot.key, i)
                else i
            )
            for i in items
        )
        return awaits, shown

    awaits, shown_items = await asyncio.to_thread(reads)
    content = welcome.render(
        language=language,
        goal=_goal(settled),
        items=shown_items,
        awaits=awaits,
        waiting=settled.waiting,
        live=live,
        checked_at=inputs.clock(checked_epoch),
        clean=_clean,
        terminal=work_item_is_terminal,
    )
    # Every ledger string was redacted as it was cleaned; this scan of the exact
    # text the row posts is the last check. A hit returns "": the caller posts
    # the quiet line instead.
    if _holds_a_secret(content):
        logger.info("warm task brief for %s held a secret after cleaning", slot.key)
        return ""
    return content
