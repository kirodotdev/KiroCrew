"""The rows ``ScriptContext.list_sessions()`` returns, projected from the slot list.

``GET /api/chat/slots`` is the sidebar's own frame and has no contract of its own.
This projection is the contract: a script reads these fields, and a rename in the
slot frame breaks ``test/test_cron_script_session_state.py`` instead of a script.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

#: The values of a row's ``status``, most urgent first. A row takes the first one
#: whose condition holds, so a running turn that asks for an approval reads as
#: ``awaiting_approval``, not ``working``.
SESSION_STATUSES = (
    "awaiting_approval",
    "awaiting_answer",
    "working",
    "awaiting_choice",
    "interrupted",
    "idle",
)


def session_state_rows(slots: list[Any]) -> list[dict[str, Any]]:
    """One row per dashboard chat a script may list, in the slot list's order.

    Incognito and temporary chats, and chats born in a channel thread, are left
    out: their owner chose not to keep them, or they live in the channel.
    """
    rows = []
    for slot in slots:
        if not isinstance(slot, dict) or not _listed(slot):
            continue
        rows.append(
            {
                "slot": str(slot.get("key") or ""),
                "title": str(slot.get("title") or slot.get("label") or ""),
                "folder_id": str(slot.get("folder_id") or ""),
                "status": _status(slot),
                "unread": _unread(slot),
                "last_activity": _ts(slot.get("last_activity_ts")),
                "last_reply": _ts(slot.get("last_agent_ts")),
            }
        )
    return rows


def _listed(slot: dict[str, Any]) -> bool:
    if not slot.get("key"):
        return False
    if (slot.get("memory_mode") or "persistent") != "persistent":
        return False
    return not (slot.get("slack_origin_channel") or slot.get("slack_origin_thread_ts"))


def _status(slot: dict[str, Any]) -> str:
    if slot.get("pending_approval"):
        return "awaiting_approval"
    if slot.get("needs_input"):
        return "awaiting_answer"
    if slot.get("running"):
        return "working"
    if slot.get("has_options"):
        return "awaiting_choice"
    if slot.get("interrupted"):
        return "interrupted"
    return "idle"


def _unread(slot: dict[str, Any]) -> bool:
    """The sidebar's badge rule with "badge only when done or waiting" off.

    A muted chat badges nothing. A row with no read epoch badges nothing either,
    as in the sidebar: the gateway has not loaded the read times yet, so it
    cannot say what was seen.
    """
    if slot.get("muted"):
        return False
    candidate = _parsed(slot.get("unread_ts"))
    epoch = _parsed(slot.get("read_epoch"))
    if candidate is None or epoch is None:
        return False
    read = _parsed(slot.get("last_read_ts"))
    floor = max(epoch, read) if read is not None else epoch
    return candidate > floor


def _ts(value: object) -> str:
    return value if isinstance(value, str) else ""


def _parsed(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # The gateway writes UTC; a naive value is read as UTC so the two compare.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
