"""The dashboard gateway's side of fair admission: process stop, queue entries, notes.

The admission gate holds no dashboard state. The gateway installs three hooks
(:class:`kiro_crew.shared_crew.admission_gate.HostHooks`) so that:

* work the gateway's own stop interrupts is recorded ``cancelled``, not
  ``completed`` (the process-shutdown flag ``chat_turn.turn_marker`` reads);
* a message parked behind a busy session expires only once its queue entry is
  gone (a person removed the card), never while it still waits its turn;
* a chat turn that reconciliation ended after a restart says so in its own
  session, so a lost reply is never silent; the note waits for the startup
  restore and opens the session from disk when no slot holds it.

Every hook runs on the event loop; only the note's delivery reads the disk.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from kiro_crew.shared_crew import admission_gate as _gate
from kiro_crew.shared_crew.admission import AcceptedWork, OperationKind, OperationState
from kiro_crew.shared_crew.admission_entry import (
    CHAT_AUTHOR_PAYLOAD_KEY,
    CHAT_PROMPT_PAYLOAD_KEY,
    CHAT_SEND_ID_PAYLOAD_KEY,
    QUEUED_WORK_META_KEY,
)
from kiro_crew.shared_crew.contracts import MESSAGE_AUTHOR_META_KEY
from kiro_crew.shared_crew.memory_changes import slot_for_session

logger = logging.getLogger(__name__)

_ERROR_CLS = "msg msg-err"
_USER_CLS = "msg msg-u"
_DASHBOARD_PREFIX = "dashboard:"
#: How many trailing user rows are compared against an accepted prompt.
_USER_ROW_SCAN = 50
#: Restart notes one process holds before the restore lets them land (bounded).
_OWED_MAX = 10000
_OWED_ATTR = "_shared_crew_owed_notes"
_DELIVERY_ATTR = "_shared_crew_note_delivery"
_RESTORED_ATTR = "_shared_crew_sessions_restored"
#: Session history keys this process has seen recorded on disk (bounded).
_RECORDED: set[str] = set()
_RECORDED_MAX = 20000

#: The note a session shows for its turn that a restart ended.
NOTE_UNKNOWN = "⚠ The Crew restarted while this message was running; its reply may be incomplete."
NOTE_NOT_STARTED = "⚠ The Crew restarted before this message started. Send it again to run it."


def shutdown_probe(state: Any) -> Callable[[], bool]:
    def stopping() -> bool:
        from kiro_crew.dashboard.chat_runner import _gateway_shutdown_requested

        return bool(_gateway_shutdown_requested(state))

    return stopping


def parked_probe(state: Any) -> Callable[[str], bool]:
    def present(work_id: str) -> bool:
        slots = getattr(state, "_slots", None)
        for slot in list(slots.values()) if isinstance(slots, dict) else ():
            for entry in getattr(slot, "_queue", None) or ():
                meta = entry.get("meta") if isinstance(entry, dict) else None
                if isinstance(meta, dict) and meta.get(QUEUED_WORK_META_KEY) == work_id:
                    return True
        return False

    return present


@dataclass(frozen=True)
class _Note:
    """A restart note owed to one session, with the prompt its work was accepted with."""

    session_key: str
    text: str
    prompt: str
    author: Mapping[str, Any] | None
    send_id: str = ""


def _restored_event(state: Any) -> asyncio.Event:
    event = getattr(state, _RESTORED_ATTR, None)
    if not isinstance(event, asyncio.Event):
        event = asyncio.Event()
        setattr(state, _RESTORED_ATTR, event)
    return event


def mark_sessions_restored(state: Any) -> None:
    """The gateway's startup restore built every slot it will build; owed notes may land.

    Reconciliation runs at gateway start, before the restore, so a note written
    then would find no slot. Notes wait for this mark.
    """
    _restored_event(state).set()


def _note_for(work: AcceptedWork) -> str | None:
    if work.operation is not OperationKind.CHAT_TURN:
        return None
    if work.extra.get(_gate.PARKED_KEY):
        # Its queue entry was saved with the session and runs again from there.
        return None
    if work.state is OperationState.UNKNOWN_OUTCOME:
        return NOTE_UNKNOWN
    if work.state is OperationState.CANCELLED and work.reason is None:
        return NOTE_NOT_STARTED
    return None


def reconcile_note(state: Any) -> Callable[[AcceptedWork, "Mapping[str, Any] | None"], None]:
    """The listener reconciliation tells of each record it ended.

    A chat turn the restart ended gets an error row in its session. The session
    is opened from disk when no slot holds it (a session the crash left without
    an open-tab record), and the accepted prompt from the work's payload is put
    back as its user row when the session never saved it. Nothing is replayed.
    Delivery waits for :func:`mark_sessions_restored`.
    """

    def note(work: AcceptedWork, payload: Mapping[str, Any] | None = None) -> None:
        text = _note_for(work)
        if text is None:
            return
        prompt = ""
        send_id = ""
        author: Mapping[str, Any] | None = None
        if isinstance(payload, Mapping):
            raw = payload.get(CHAT_PROMPT_PAYLOAD_KEY)
            prompt = raw if isinstance(raw, str) else ""
            raw_author = payload.get(CHAT_AUTHOR_PAYLOAD_KEY)
            author = raw_author if isinstance(raw_author, Mapping) else None
            raw_id = payload.get(CHAT_SEND_ID_PAYLOAD_KEY)
            send_id = raw_id if isinstance(raw_id, str) else ""
        owed = getattr(state, _OWED_ATTR, None)
        if not isinstance(owed, list):
            owed = []
            setattr(state, _OWED_ATTR, owed)
        if len(owed) >= _OWED_MAX:
            logger.warning("shared crew: restart note for %s dropped (bound)", work.session_key)
            return
        owed.append(_Note(work.session_key, text, prompt, author, send_id))
        task = getattr(state, _DELIVERY_ATTR, None)
        if task is None or task.done():
            setattr(state, _DELIVERY_ATTR, asyncio.ensure_future(deliver_owed_notes(state)))

    return note


async def deliver_owed_notes(state: Any) -> int:
    """Write every owed restart note once the startup restore is done; how many landed."""
    await _restored_event(state).wait()
    owed = getattr(state, _OWED_ATTR, None)
    delivered = 0
    while isinstance(owed, list) and owed:
        item = owed.pop(0)
        try:
            if await _deliver(state, item):
                delivered += 1
        except Exception:  # noqa: BLE001 -- the note is best effort
            logger.warning("shared crew: restart note failed", exc_info=True)
    return delivered


async def _deliver(state: Any, item: _Note) -> bool:
    slot = slot_for_session(state, item.session_key)
    if slot is None and item.session_key.startswith(_DASHBOARD_PREFIX):
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        slot = await rehydrate_slot_from_history_async(
            state, item.session_key.removeprefix(_DASHBOARD_PREFIX)
        )
    if slot is None:
        logger.warning(
            "shared crew: no session %s for its restart note; the turn ended %s",
            item.session_key,
            "unknown_outcome" if item.text == NOTE_UNKNOWN else "cancelled",
        )
        return False
    if item.prompt and not _holds_user_row(slot, item.prompt, item.send_id):
        meta: dict[str, Any] = {}
        if item.send_id:
            meta[CHAT_SEND_ID_PAYLOAD_KEY] = item.send_id
        if item.author is not None:
            meta[MESSAGE_AUTHOR_META_KEY] = dict(item.author)
        slot.append("user", item.prompt, _USER_CLS, meta=meta or None)
    slot.append("error", item.text, _ERROR_CLS)
    return True


def _holds_user_row(slot: Any, prompt: str, send_id: str = "") -> bool:
    """Match the accepted send's identity; only legacy payloads match by text."""
    rows = getattr(slot, "messages", None)
    if not isinstance(rows, list):
        return False
    for row in rows[-_USER_ROW_SCAN:]:
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        if send_id:
            meta = row.get("meta")
            if isinstance(meta, Mapping) and meta.get(CHAT_SEND_ID_PAYLOAD_KEY) == send_id:
                return True
        elif row.get("content") == prompt:
            return True
    return False


async def ensure_slot_recorded(state: Any, slot: Any) -> bool:
    """Write a never-saved session's metadata line before its work is accepted.

    Accepted work is durable at once while a new session's first save is
    deferred; writing the line first means a crash in between cannot leave a
    work record naming a session that is gone. A session that already has a
    line is left as it is.
    Off-loop write; True when the session has a line afterwards.
    """
    log = getattr(state, "conversation_log", None)
    if log is None:
        return False
    from kiro_crew.dashboard.chat_utils import drained_to_thread, slot_history_key
    from kiro_crew.dashboard.slot_persistence import metadata_codec

    try:
        history_key = slot_history_key(slot)
        if history_key in _RECORDED:
            return True
        line = {
            "_type": "metadata",
            **metadata_codec.encode(
                slot,
                folds=metadata_codec.SaveFolds(
                    memory_mode=getattr(slot, "memory_mode", "persistent"),
                    created_at=slot.created_at,
                    tab_id=getattr(slot, "_tab_id", None),
                ),
            ),
        }
        written = await drained_to_thread(
            log.update_metadata_if, history_key, line, lambda meta: not meta
        )
        if not written:
            # Skipped: either a line already exists (recorded) or the read failed.
            meta, readable = await drained_to_thread(log.get_metadata_status, history_key)
            if not (readable and meta):
                return False
    except Exception:  # noqa: BLE001 -- the send still runs; the periodic save retries
        logger.warning("shared crew: could not record session %s", slot.key, exc_info=True)
        return False
    _RECORDED.add(history_key)
    while len(_RECORDED) > _RECORDED_MAX:
        _RECORDED.pop()
    return True


def child_hooks(state: Any) -> tuple[Any, Any]:
    """The subagent run probe and stop hook, resolving ``state.subagents`` per call.

    The manager may be built after the hooks are installed; until it exists the
    probe answers "still running" and the stop hook does nothing.
    """
    import asyncio

    from kiro_crew.shared_crew import admission_entry

    try:
        loop: Any = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def probe(agent_id: str) -> Any:
        manager = getattr(state, "subagents", None)
        if manager is None:
            return None
        return admission_entry.subagent_probe(manager)(agent_id)

    def cancel(work: Any) -> None:
        manager = getattr(state, "subagents", None)
        if manager is None or loop is None:
            return
        admission_entry.subagent_cancel(manager, loop)(work)

    return probe, cancel


def install(state: Any) -> None:
    """Install the gateway's hooks on every admission service it builds."""
    probe, cancel = child_hooks(state)
    _gate.install_host_hooks(
        _gate.HostHooks(
            shutting_down=shutdown_probe(state),
            parked_present=parked_probe(state),
            reconciled=reconcile_note(state),
            child_probe=probe,
            child_cancel=cancel,
        )
    )


def uninstall() -> None:
    _gate.install_host_hooks(None)


__all__ = [
    "child_hooks",
    "deliver_owed_notes",
    "ensure_slot_recorded",
    "mark_sessions_restored",
    "NOTE_NOT_STARTED",
    "NOTE_UNKNOWN",
    "install",
    "parked_probe",
    "reconcile_note",
    "shutdown_probe",
    "uninstall",
]
