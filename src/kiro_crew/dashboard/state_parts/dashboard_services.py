"""The services ``DashboardState`` hosts, and the coordinators it reaches them through.

The lazy coordinator accessors (``_approvals_for`` .. ``_registry_for``) a
``__new__``-built state still resolves, the background-turn budget, approvals
and questions, notifications, the periodic flush, context snapshots and yolo.

Instance fields of ``DashboardState`` this module reads and writes:

Writes: ``_bg_turn_cap``, ``_bg_turn_sema``, ``_bg_turns_running``,
``_bg_turns_waiting``.
Reads only: ``MAX_BACKGROUND_TURNS``, ``MAX_BACKGROUND_TURNS_CEIL``,
``_APPROVAL_TIMEOUT``, ``_BACKGROUND_APPROVAL_TIMEOUT_SECS``,
``_BACKGROUND_QUEUE_WAIT_SECS``, ``_approval_futures``, ``_log``,
``_pending_approvals``, ``_slots``.

A function whose first parameter is ``self`` is a ``DashboardState`` method: the
class binds it under the same name (``state_parts/dashboard_state.py``), so it reads
and writes the state's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import _raw_config, config_dir
from kiro_crew.dashboard.dashboard_persistence import DashboardPersistenceCoordinator
from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator, QuestionCoordinator
from kiro_crew.dashboard.notification_coordinator import (
    NATIVE_TAG_MAX_BYTES,
    NotificationCoordinator,
    _serialized_bytes,
    native_notification_tag,
)
from kiro_crew.dashboard.slot_registry import SlotRegistry
from kiro_crew.dashboard.state_parts.notification_log import (
    _MAX_PERSISTED_NOTIFICATIONS,
    _notification_io_executor,
    _persist_notification,
    _redact_note_value,
    _rewrite_notifications,
    sweep_expired_notifications,
)
from kiro_crew.dashboard.state_parts.transcript_rows import _mark_permission_resolved
from kiro_crew.dashboard.turn_dispatch import BackgroundTurnAbandoned, mark_turn_unstarted
from kiro_crew.dashboard.websocket_hub import WebSocketHub
from kiro_crew.notifications.bus import NotificationValidationError, payload_from_legacy
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.safety_override import safety_override
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")


#: Push services reject an encrypted payload larger than 4 KB with 413, which
#: the fan-out treats as a transient, non-gone failure it never retries — so an
#: oversized note silently never reaches the closed device this feature exists
#: for. The notification bus permits a 20,000-char body (``_MAX_BODY_LEN``), and
#: a multibyte (CJK) body multiplies to ~3x that in UTF-8, far past the ceiling.
#: Every string field of the payload gets its own budget in SERIALIZED bytes:
#: the UTF-8 length of its JSON-escaped form, as ``send_web_push`` encodes it
#: (``ensure_ascii=False``), so a quote, backslash or control character that
#: escapes to 2 or 6 bytes is counted at that size. The budgets plus the fixed
#: JSON envelope (about 110 bytes) stay under ``_PUSH_PAYLOAD_BYTES``, which is
#: the 4096 byte ceiling minus the aes128gcm framing (86 byte header, 16 byte
#: tag, 1 byte padding delimiter) and a margin. The deep link is sent twice
#: (``navigate`` and ``data.url``), so it is counted twice.
_PUSH_PAYLOAD_BYTES = 3800
_PUSH_TITLE_BYTES = 256
_PUSH_BODY_BYTES = 2000
_PUSH_CHANNEL_BYTES = 128
_PUSH_TS_BYTES = 64
_PUSH_URL_BYTES = 384


def _truncate_bytes(text: str, byte_limit: int) -> str:
    """Cap *text* so its serialized form is at most *byte_limit* bytes.

    Counts each character at its JSON-escaped UTF-8 size (a CJK char is 3
    bytes, a control character up to 6) and cuts only between characters, so
    no multibyte sequence or escape is split. Marks a cut with an ellipsis,
    which itself fits inside the budget.
    """
    if _serialized_bytes(text) <= byte_limit:
        return text
    budget = max(0, byte_limit - _serialized_bytes("…"))
    used = 0
    end = 0
    for ch in text:
        cost = _serialized_bytes(ch)
        if used + cost > budget:
            break
        used += cost
        end += 1
    return text[:end] + "…"


def _web_push_payload(note: dict[str, Any]) -> dict[str, Any]:
    """Build the declarative Web Push JSON for one delivered note.

    Shape follows WebKit's Declarative Web Push (``web_push: 8030`` +
    ``notification``): on iOS/Safari 18.4+ the payload renders natively with no
    service-worker push handler, while Chromium/Firefox receive the identical
    JSON in a classic ``push`` event and read the same ``notification`` object.
    The ``navigate`` URL is a same-origin dashboard path (the deep link the note
    already carries, defaulting to the dashboard root) so a click reopens the
    right place; ``data`` echoes the note timestamp for click bookkeeping.

    Every string field is held to a serialized byte budget so the encrypted
    payload stays under the 4 KB Web Push ceiling: an oversized push is
    rejected with 413 and never arrives. Text fields are truncated; a deep link
    or timestamp over budget is replaced (by the dashboard root, or dropped),
    because a cut link or id would point nowhere.
    """
    title = _truncate_bytes(note.get("title") or "Kiro Crew", _PUSH_TITLE_BYTES)
    # The body is markdown; an OS banner shows text literally. Flatten it the
    # way the open tab's banner does (stripMd) before the byte cut, so the push
    # banner that replaces the tab's one under the shared tag reads the same.
    # Flattening drops format characters and markup, which can JOIN a credential
    # that a zero-width character or emphasis had split past the delivery-time
    # redaction, so the flattened text is redacted again before it leaves the
    # host (strip before redact: preview_text.drop_format_chars).
    raw_body = note.get("body")
    flat_body = strip_markdown_preview(raw_body) if isinstance(raw_body, str) else ""
    body = _truncate_bytes(_redact_note_value(flat_body), _PUSH_BODY_BYTES)
    navigate = str(note.get("url") or "/")
    if _serialized_bytes(navigate) > _PUSH_URL_BYTES:
        navigate = "/"
    ts = note.get("ts")
    if not isinstance(ts, str) or _serialized_bytes(ts) > _PUSH_TS_BYTES:
        ts = None
    channel = note.get("channel")
    if channel is not None:
        channel = _truncate_bytes(str(channel), _PUSH_CHANNEL_BYTES)
    notification: dict[str, Any] = {
        "title": title,
        "body": body,
        "navigate": navigate,
        "data": {"ts": ts, "channel": channel, "url": navigate},
    }
    # The tag the coordinator stamped on the note, so the service worker's
    # banner replaces (not duplicates) the one an open tab already showed.
    tag = note.get("tag")
    if not isinstance(tag, str) or not tag or _serialized_bytes(tag) > NATIVE_TAG_MAX_BYTES:
        tag = native_notification_tag(note)
    notification["tag"] = tag
    return {"web_push": 8030, "notification": notification}


def _new_notification_coordinator(
    web_push_fanout: Callable[[dict[str, Any]], None] | None = None,
) -> NotificationCoordinator:
    """Build a coordinator whose providers resolve facade seams at call time."""
    return NotificationCoordinator(
        logger_provider=lambda: logger,
        payload_from_legacy=lambda *args, **kwargs: payload_from_legacy(*args, **kwargs),
        validation_error=NotificationValidationError,
        redact_value=lambda value: _redact_note_value(value),
        sweep_expired=lambda notes: sweep_expired_notifications(notes),
        persist_one=lambda note: _persist_notification(note),
        rewrite_all=lambda notes: _rewrite_notifications(notes),
        executor_provider=lambda: _notification_io_executor(),
        max_persisted=_MAX_PERSISTED_NOTIFICATIONS,
        web_push_fanout=web_push_fanout,
    )


def _notifications_for(state: Any) -> NotificationCoordinator:
    """Return the per-state coordinator, including for ``__new__`` test states."""
    coordinator = getattr(state, "_notification_coordinator", None)
    if not isinstance(coordinator, NotificationCoordinator):
        coordinator = _new_notification_coordinator()
        state._notification_coordinator = coordinator
    return coordinator


def _approvals_for(state: Any) -> ApprovalCoordinator:
    coordinator = getattr(state, "_approval_coordinator", None)
    if not isinstance(coordinator, ApprovalCoordinator):
        coordinator = ApprovalCoordinator()
        state._approval_coordinator = coordinator
    return coordinator


def _questions_for(state: Any) -> QuestionCoordinator:
    coordinator = getattr(state, "_question_coordinator", None)
    if not isinstance(coordinator, QuestionCoordinator):
        coordinator = QuestionCoordinator()
        state._question_coordinator = coordinator
    return coordinator


def _permission_marker() -> Callable[[list[dict], str, str], bool]:
    """Resolve the module-level marker lazily for monkeypatched tests/callers."""
    return _mark_permission_resolved


def _new_websocket_hub(state: Any) -> WebSocketHub:
    """Build a hub whose providers resolve facade seams at call time."""
    return WebSocketHub(
        state,
        serving_loop_provider=lambda: state.serving_loop,
        running_loop_provider=lambda: state._running_loop(),
        logger_provider=lambda: logger,
        scope_state_provider=lambda: state,
    )


def _websocket_for(state: Any) -> WebSocketHub:
    """Return the per-state hub, including for ``__new__`` test states."""
    hub = getattr(state, "_websocket_hub", None)
    if not isinstance(hub, WebSocketHub):
        hub = _new_websocket_hub(state)
        state._websocket_hub = hub
    return hub


def _new_dashboard_persistence() -> DashboardPersistenceCoordinator:
    """Build persistence providers that preserve module monkeypatch seams."""
    return DashboardPersistenceCoordinator(
        config_dir_provider=lambda: config_dir(),
        atomic_write_provider=lambda: atomic_write,
        logger_provider=lambda: logger,
        json_codec_provider=lambda: json,
        wall_time_provider=lambda: time.time(),
    )


def _persistence_for(state: Any) -> DashboardPersistenceCoordinator:
    """Return the per-state coordinator, including for ``__new__`` test states."""
    coordinator = getattr(state, "_persistence_coordinator", None)
    if not isinstance(coordinator, DashboardPersistenceCoordinator):
        coordinator = _new_dashboard_persistence()
        state._persistence_coordinator = coordinator
    return coordinator


def _registry_for(state: Any) -> SlotRegistry:
    """Return the per-state registry boundary for facade-compatible test states."""
    registry = getattr(state, "_slot_registry", None)
    if not isinstance(registry, SlotRegistry):
        registry = SlotRegistry()
        state._slot_registry = registry
    return registry


def approval_timeout_for(self: DashboardState, slot: "_ChatSlot") -> float:
    """Approval window for an interactive tool prompt raised inside *slot*.

    FIX 1. The dashboard runner waits on its OWN per-slot future rather than
    going through :meth:`request_approval`, so it never reached the
    deny-fast background branch: every unattended app worker that tripped
    one untrusted tool held its slot for the full
    ``_APPROVAL_TIMEOUT`` (2h) and then denied anyway — two hours of a
    worker's life spent parked, with nothing on screen to explain it.

    Returning the SAME two constants ``request_approval`` uses is the point:
    the previous bug was a hardcoded ``7200.0`` at the call site, which
    could not track either constant. See :attr:`_ChatSlot.unattended` for
    why app-ownership is the detector.
    """
    if slot.unattended:
        return float(self._BACKGROUND_APPROVAL_TIMEOUT_SECS)
    return float(self._APPROVAL_TIMEOUT)


def effective_max_background_turns(self: DashboardState) -> int:
    """Configured cap on concurrent unattended turns.

    Reads ``config.json → dashboard.max_background_turns`` (same
    ``_raw_config`` route ``sandbox.py`` and ``mcp_gateway/pool.py`` use for
    their tunables) and clamps to ``[1, MAX_BACKGROUND_TURNS_CEIL]`` so an
    operator can widen the fleet without editing code but can never remove
    the bound. Unreadable/garbage config falls back to the default rather
    than failing a turn.
    """
    try:
        raw = (_raw_config().get("dashboard") or {}).get(
            "max_background_turns", self.MAX_BACKGROUND_TURNS
        )
        val = int(raw)
    except Exception:
        self._log.debug("background-turn cap config unavailable; using default", exc_info=True)
        val = self.MAX_BACKGROUND_TURNS
    return max(1, min(val, self.MAX_BACKGROUND_TURNS_CEIL))


def _background_turn_sema(self: DashboardState) -> asyncio.Semaphore:
    """The cap's semaphore, created on first use and resized when idle.

    Lazy because ``DashboardState`` is constructed before the event loop in
    some hosts (tests, CLI) and ``asyncio.Semaphore`` binds to the running
    loop. Resized only while nothing is in flight: in-flight holders own
    permits on the object they entered, so swapping under them would let the
    cap be exceeded by the difference.
    """
    eff = self.effective_max_background_turns()
    if self._bg_turn_sema is None:
        self._bg_turn_sema = asyncio.Semaphore(eff)
        self._bg_turn_cap = eff
    elif eff != self._bg_turn_cap and not (self._bg_turns_running or self._bg_turns_waiting):
        self._bg_turn_sema = asyncio.Semaphore(eff)
        self._bg_turn_cap = eff
    return self._bg_turn_sema


def background_turn_stats(self: DashboardState) -> dict[str, int]:
    """Cap / in-flight / queued counts — the cap's observability surface.

    Surfaced in the status payload and asserted by tests, so "the fleet is
    queued behind the cap" is a readable state rather than an invisible
    stall that looks like a hung worker.
    """
    return {
        "cap": self._bg_turn_cap or self.effective_max_background_turns(),
        "running": self._bg_turns_running,
        "waiting": self._bg_turns_waiting,
    }


async def run_background_turn(self: DashboardState, slot: "_ChatSlot", coro: Any) -> Any:
    """Await *coro* under the unattended-turn cap.

    QUEUES rather than rejects at the cap: a rejected crew turn loses the
    issue it was mid-way through, while a queued one only starts late. An
    attended slot is passed straight through, so this wrapper is inert for
    every human session and adds no semaphore traffic to the interactive
    path.
    """
    if not slot.unattended:
        return await coro
    sema = self._background_turn_sema()
    queued = sema.locked()
    if queued:
        self._bg_turns_waiting += 1
        # info, not debug: this is the difference between "the fleet is
        # throttled" and "a worker is hung", and it is the only signal a
        # queued turn emits before it starts.
        self._log.info(
            "background turn queued behind the cap: slot=%s cap=%d running=%d waiting=%d",
            slot.key,
            self._bg_turn_cap,
            self._bg_turns_running,
            self._bg_turns_waiting,
        )
    try:
        await asyncio.wait_for(sema.acquire(), timeout=self._BACKGROUND_QUEUE_WAIT_SECS)
    except asyncio.TimeoutError:
        coro.close()
        mark_turn_unstarted(slot)
        self._log.warning(
            "background turn abandoned after waiting %ds for a permit: slot=%s cap=%d",
            self._BACKGROUND_QUEUE_WAIT_SECS,
            slot.key,
            self._bg_turn_cap,
        )
        # Its own TimeoutError, so the dispatch reports a turn that never ran
        # rather than one the turn ceiling stopped.
        raise BackgroundTurnAbandoned(self._BACKGROUND_QUEUE_WAIT_SECS, self._bg_turn_cap) from None
    except BaseException:
        # Cancelled while queued: the turn never ran, so close its coroutine
        # rather than leaving an un-awaited coroutine warning behind.
        coro.close()
        mark_turn_unstarted(slot)
        raise
    finally:
        if queued:
            self._bg_turns_waiting -= 1
    self._bg_turns_running += 1
    try:
        return await coro
    finally:
        self._bg_turns_running -= 1
        sema.release()


def enable_yolo(self: DashboardState, *, from_config: bool = False) -> None:
    """Activate safety override (delegates to safety_override module)."""
    source = "config" if from_config else "dashboard"
    safety_override().activate(source)


def disable_yolo(self: DashboardState) -> None:
    """Deactivate safety override (delegates to safety_override module)."""
    safety_override().deactivate("dashboard")


def is_yolo_active(self: DashboardState) -> bool:
    """Return whether safety override is active (delegates to safety_override module)."""
    return safety_override().is_active()


async def request_approval(
    self: DashboardState,
    approval_id: str,
    source: str,
    tool: str,
    *,
    tool_input: str = "",
    tool_purpose: str = "",
    slot: str = "",
    is_background: bool = False,
) -> bool:
    """Request interactive approval and deny on timeout or cancellation."""
    return await _approvals_for(self).request(
        self,
        approval_id,
        source,
        tool,
        tool_input=tool_input,
        tool_purpose=tool_purpose,
        slot=slot,
        is_background=is_background,
        redact_url=redact_exfiltration_urls,
        redact_secret=redact_credentials,
    )


def linked_session_busy(self: DashboardState, session_key: str) -> bool:
    """Whether *session_key*'s turn lease is held right now.

    The same lease a channel dispatcher checks before it starts or queues a
    turn (``SessionManager.is_busy``): held for the whole turn by whichever
    path runs it, released when it ends. A slot bound to that session reads it
    through ``_ChatSlot.linked_turn_running`` so a channel-path turn is not
    summarised as interrupted. Never raises; no session manager answers False.
    """
    sessions = getattr(self, "sessions", None)
    is_busy = getattr(sessions, "is_busy", None)
    if not session_key or not callable(is_busy):
        return False
    try:
        return is_busy(session_key) is True
    except Exception:
        logger.debug("linked_session_busy: probe failed for %s", session_key, exc_info=True)
        return False


def pending_coordinator_approvals(self: DashboardState, slot_key: str) -> list[dict]:
    """Live coordinator approvals owned by *slot_key*, oldest first.

    A record counts only while its state-level future is still open: a
    resolved or expired approval whose ``finally`` has not yet popped the
    record must not keep the slot in the Needs Approval lane. An approval
    with no owning slot belongs to no slot and is never returned.
    """
    if not slot_key:
        return []
    records = getattr(self, "_pending_approvals", None) or {}
    futures = getattr(self, "_approval_futures", None) or {}
    pending: list[dict] = []
    for approval_id, record in records.items():
        if record.get("slot") != slot_key:
            continue
        future = futures.get(approval_id)
        if future is None or future.done():
            continue
        pending.append(record)
    return pending


def _audit_and_broadcast_approval(
    self: DashboardState,
    session_key: str,
    approval_id: str,
    approved: bool,
    decision: str = "",
) -> None:
    """Audit and broadcast one approval decision."""
    _approvals_for(self).audit_and_broadcast(
        self,
        session_key,
        approval_id,
        approved,
        decision,
        audit_provider=sel,
    )


def _audit_approval(
    self: DashboardState, session_key: str, approval_id: str, approved: bool, decision: str = ""
) -> None:
    """Audit one approval outcome with no broadcast."""
    _approvals_for(self).audit(
        self, session_key, approval_id, approved, decision, audit_provider=sel
    )


def resolve_state_approval(self: DashboardState, approval_id: str, approved: bool) -> bool:
    """Resolve only a state-level background approval."""
    return _approvals_for(self).resolve_state(self, approval_id, approved)


def resolve_approval(
    self: DashboardState,
    approval_id: str,
    approved: bool,
    *,
    rejected_once: bool = False,
) -> bool:
    """Resolve one state- or slot-level approval without widening authority."""
    return _approvals_for(self).resolve(
        self,
        approval_id,
        approved,
        rejected_once=rejected_once,
        permission_marker=_permission_marker(),
    )


def resolve_slot_approval(
    self: DashboardState,
    slot: Any,
    approval_id: str,
    approved: bool,
    *,
    rejected_once: bool = False,
    expected_future: asyncio.Future[str] | None = None,
) -> bool:
    """Resolve one approval on *slot*'s own future, skipping state-level approvals.

    *expected_future* pins the one request the caller judged: the call fails
    when the slot now holds a different future under the same id.
    """
    return _approvals_for(self).resolve_on_slot(
        self,
        slot,
        approval_id,
        approved,
        rejected_once=rejected_once,
        permission_marker=_permission_marker(),
        expected_future=expected_future,
    )


def _redact_questions(self: DashboardState, questions: list[dict]) -> list[dict]:
    """Redact questions and reject text collisions introduced by redaction."""
    return _questions_for(self).redact_questions(
        questions,
        redact_url=redact_exfiltration_urls,
        redact_secret=redact_credentials,
    )


async def post_question_card(
    self: DashboardState, slot_key: str, questions: list[dict], *, native: bool = False
) -> int:
    """Post a non-blocking owner-only question card.

    ``native`` marks kiro-cli's own mid-turn ``AskUserQuestion`` card; see
    ``QuestionCoordinator.post_card``.
    """
    return await _questions_for(self).post_card(self, slot_key, questions, native=native)


def mark_question_pending(
    self: DashboardState,
    slot_key: str,
    *,
    blocking: bool,
    card_id: str,
    questions: list[dict] | None = None,
    native: bool = False,
) -> None:
    """Record one unanswered question and push the slot status."""
    _questions_for(self).mark_pending(
        self,
        slot_key,
        blocking=blocking,
        card_id=card_id,
        questions=questions,
        native=native,
    )
    slot = self._slots.get(slot_key)
    if slot is not None:
        self.notify_dashboard_card(slot, "question")
    # Opening a question can land after its tool call's log growth settled (a
    # retried open POST), so tell the feed directly, as retiring one does.
    from kiro_crew.dashboard import crewmate_feed

    crewmate_feed.question_retired(slot_key)


def clear_question_pending(
    self: DashboardState,
    slot_key: str,
    *,
    blocking: bool | None = None,
    card_id: str | None = None,
    reason: str | None = None,
) -> bool:
    """Retire only question records matching both supplied filters."""
    cleared = _questions_for(self).clear_pending(
        self,
        slot_key,
        blocking=blocking,
        card_id=card_id,
        reason=reason,
    )
    if cleared:
        # A crewmate page's waiting fold is fed on log growth, and retiring a
        # question writes no log entry: tell the feed directly.
        from kiro_crew.dashboard import crewmate_feed

        crewmate_feed.question_retired(slot_key)
    return cleared


def _broadcast_question_retired(
    self: DashboardState,
    slot_key: str,
    cards: list[str] | list[tuple[str, bool]],
    *,
    reason: str | None = None,
) -> None:
    """Tell owner clients that question cards are retired."""
    normalized = [(card, False) if isinstance(card, str) else card for card in cards]
    _questions_for(self).broadcast_retired(self, slot_key, normalized, reason=reason)


def _push_slots(self: DashboardState) -> None:
    """Push question status without failing the question lifecycle."""
    _questions_for(self).push_slots(self)


def recent_question_resolutions(self: DashboardState) -> dict[str, str]:
    """Return bounded recent blocking ask endings for reconnect repair."""
    return _questions_for(self).recent_resolutions(self)


async def request_question(
    self: DashboardState,
    ask_id: str,
    slot_key: str,
    questions: list[dict],
    timeout: int | None = None,
) -> dict[str, str] | None:
    """Run the legacy blocking owner question round-trip."""
    return await _questions_for(self).request(
        self,
        ask_id,
        slot_key,
        questions,
        timeout,
    )


def resolve_question(
    self: DashboardState,
    ask_id: str,
    answers: dict[str, str] | None,
    *,
    reason: str | None = None,
    responder: Any = None,
    response_id: str = "",
) -> bool:
    """Resolve one blocking question future if it is still pending.

    ``reason`` says why a ``None`` answer ended the wait (``dismissed``,
    ``composer``, ...); only a blocking MCP ask reports it back to the agent.
    ``responder`` is the verified Shared Crew principal answering through the
    dashboard; with it the resolution goes through the shared decision
    registry (first valid response wins once).
    """
    return _questions_for(self).resolve(
        self, ask_id, answers, reason=reason, responder=responder, response_id=response_id
    )


async def open_agent_ask(
    self: DashboardState, ask_id: str, slot_key: str, session_key: str, questions: list[dict]
) -> int | None:
    """Open a blocking MCP ask, or return ``None`` when live asks fill the cap."""
    return await _questions_for(self).open_agent_ask(self, ask_id, slot_key, session_key, questions)


async def wait_agent_ask(
    self: DashboardState, ask_id: str, session_key: str, slice_secs: float
) -> dict | None:
    """Wait one slice for an agent ask; ``None`` when the caller does not own it."""
    return await _questions_for(self).wait_agent_ask(self, ask_id, session_key, slice_secs)


def withdraw_agent_ask(self: DashboardState, ask_id: str, session_key: str) -> bool:
    """Withdraw an agent ask whose tool call was cancelled."""
    return _questions_for(self).withdraw_agent_ask(self, ask_id, session_key)


def cancel_questions_for_slot(self: DashboardState, slot_key: str) -> int:
    """Unblock every blocking question owned by one slot."""
    return _questions_for(self).cancel_for_slot(self, slot_key)


def start_flush_loop(self: DashboardState) -> None:
    _persistence_for(self).start_flush_loop(self)


async def _flush_loop(self: DashboardState) -> None:
    await _persistence_for(self)._flush_loop(self)


def flush_slot_now(self: DashboardState, slot: _ChatSlot) -> None:
    _persistence_for(self).flush_slot_now(self, slot)


def _flush_dirty_slots(self: DashboardState) -> None:
    _persistence_for(self)._flush_dirty_slots(self)


def _persist_open_slots(self: DashboardState) -> None:
    _persistence_for(self)._persist_open_slots(self)


def notify(
    self: DashboardState,
    kind: str,
    title: str,
    body: str,
    *,
    meta: dict | None = None,
    url: str | None = None,
    actions: list[dict[str, Any]] | None = None,
    channel: str | None = None,
    member: dict[str, str] | None = None,
    publisher_session: str | None = None,
) -> None:
    """Validate and deliver a legacy notification without raising.

    ``channel`` overrides the system channel ``kind`` maps to (see
    :func:`payload_from_legacy`); ``kind`` still reaches the frontend.
    ``member`` attributes the note to the crewmate that published it.
    ``publisher_session`` is the attested session publishing it; its mute applies.
    """
    _notifications_for(self).notify(
        self,
        kind,
        title,
        body,
        meta=meta,
        url=url,
        actions=actions,
        channel=channel,
        member=member,
        publisher_session=publisher_session,
    )


def _fan_out_web_push(self: DashboardState, note: dict[str, Any]) -> None:
    """Schedule Web Push delivery of a note to every browser subscription.

    Called from the delivery sink (synchronous). Web Push is network I/O, so
    this only schedules the send and returns. On the event loop it creates a
    task there. Off the loop (a note raised from a worker thread, such as
    ``asyncio.to_thread(state.notify, ...)``) it hands the send to the serving
    loop with ``run_coroutine_threadsafe``. Only a state that has never seen a
    loop (a synchronous unit test) skips the push. Best-effort by contract: a
    failed push must never affect the in-app notification that already landed.
    """
    running = self._running_loop()
    if running is not None:
        # Held until done: the loop keeps only a weak reference to a task.
        task = running.create_task(self._send_web_push_all(note))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return
    loop = self.serving_loop
    if loop is None or loop.is_closed():
        logger.debug("Web push skipped: no event loop to schedule it on")
        return
    asyncio.run_coroutine_threadsafe(self._send_web_push_all(note), loop)


async def _send_web_push_all(self: DashboardState, note: dict[str, Any]) -> None:
    """Encrypt-and-POST a note to every stored subscription; prune dead ones."""
    # Imported here, not at module top: web_push pulls in `cryptography`, and
    # the dashboard state sits on the tool-approval import chain
    # (slack.gateway -> dashboard.server -> … -> state). A top-level import
    # would drag the native wheel onto that chain and break every approval on
    # a host whose cryptography wheel does not load
    # (test_approval_chain_no_cryptography). The fan-out is the only user.
    try:
        from kiro_crew.notifications.web_push import (
            PushResult,
            public_push_session,
            send_web_push,
        )

        # Offloaded: the first read loads the subscriptions file.
        subscriptions = await asyncio.to_thread(self.push_subscription_store.all)
        if not subscriptions:
            return
        payload = _web_push_payload(note)
        async with public_push_session() as session:
            results = await asyncio.gather(
                *(send_web_push(session, sub, payload) for sub in subscriptions),
                return_exceptions=True,
            )
        for sub, result in zip(subscriptions, results):
            # A 404/410 is the push service's authoritative "this subscription
            # is dead" (RFC 8030); drop it so the store does not accumulate
            # tombstones and re-POST to it every notification. remove() does an
            # atomic file write, so offload it rather than blocking the loop.
            if isinstance(result, PushResult) and result.gone:
                await asyncio.to_thread(self.push_subscription_store.remove, sub["endpoint"])
    except Exception:
        # The task is fire and forget, so an error here is logged rather than
        # left as an unretrieved task exception; the in-app note already landed.
        logger.warning("Web push fan-out failed", exc_info=True)


def _deliver_note(self: DashboardState, note: dict[str, Any]) -> None:
    """Deliver one bus-validated note to memory, clients, and disk."""
    _notifications_for(self).deliver(self, note)


def register_sse(self: DashboardState) -> asyncio.Queue[dict[str, Any]]:
    """Register a new SSE client and return its dedicated queue."""
    return _notifications_for(self).register_sse(self)


def unregister_sse(self: DashboardState, q: asyncio.Queue[dict[str, Any]]) -> None:
    """Remove an SSE client queue on disconnect."""
    _notifications_for(self).unregister_sse(self, q)


async def _rewrite_notifications_async(self: DashboardState) -> None:
    """Rewrite notification storage after earlier FIFO append jobs."""
    await _notifications_for(self).rewrite(self)


async def delete_notification(self: DashboardState, ts: str) -> bool:
    """Remove a single notification by timestamp and persist to disk."""
    return await _notifications_for(self).delete(self, ts)


async def ack_notification(self: DashboardState, ts: str) -> bool:
    """Mark a notification as acknowledged and persist it."""
    return await _notifications_for(self).set_acknowledged(self, ts, acknowledged=True)


async def unack_notification(self: DashboardState, ts: str) -> bool:
    """Mark a notification as unread and persist it."""
    return await _notifications_for(self).set_acknowledged(self, ts, acknowledged=False)


async def resolve_skill_review_notifications(
    self: DashboardState, slug: str, consumed_at: str
) -> int:
    """Acknowledge the consumed generation of one skill review."""
    return await _notifications_for(self).resolve_skill_reviews(self, slug, consumed_at)


async def clear_notifications(self: DashboardState) -> None:
    """Clear memory and clients before awaiting the durable rewrite."""
    await _notifications_for(self).clear(self)


def ensure_context_snapshots_loaded(self: DashboardState) -> bool:
    return _persistence_for(self).ensure_context_snapshots_loaded(self)


def context_snapshot_for(self: DashboardState, slot_key: str) -> dict | None:
    return _persistence_for(self).context_snapshot_for(self, slot_key)


def _persist_context_snapshots(self: DashboardState) -> None:
    _persistence_for(self)._persist_context_snapshots(self)
