"""Which rows ``slack.show_in_session_list`` leaves out of the person's session list.

With the setting off, every row of a conversation that STARTED in Slack carries
``slack_list_hidden: True`` (stamped by ``DashboardState.serialize_slots``), and
the dashboard's one person-list filter (``crewModeView`` in
``website/src/lib/crewMode.ts``) leaves it out, the way it leaves out a Crew Mode
worker, except the session open in the main pane and one waiting on the person (a
tool approval or a question), which keep their rows (``slackRowHidden``). Nothing
else changes: the slot stays registered, the conversation keeps
running in Slack, History and search still list it, its approvals and
notifications still reach the person, and turning the setting back on drops the
stamp from the next slot list. Nothing is closed, written or deleted.

A Slack-born row is a slot the dashboard surfaced from a ``slack:`` channel
session (``channel_origin`` with a ``slack`` key). A dashboard-born session linked
to a Slack thread, and a Slack code channel's session, are dashboard sessions with
dashboard keys, so they are never stamped.

The setting is read from the config watcher's last applied snapshot: memory only,
because this runs on every slot-list serialization on the event loop. A missing
snapshot (no watcher in this process), a degraded ``slack`` section (which holds
the field default) or any read failure answers "listed", the behaviour before the
setting existed. A Settings save pushes a fresh slot list once the watcher has
applied it.

shortcut: a hand edit of ``config.json`` reaches the list on the next slot-list
push, not at once; subscribe a push to the watcher if that lag ever matters.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew.messaging.link import channel_namespace_of

#: Row field the person's list filter drops a row on.
SLACK_LIST_HIDDEN_KEY = "slack_list_hidden"


def slack_rows_listed() -> bool:
    """``slack.show_in_session_list`` as last applied; True unless it is a real False."""
    try:
        from kiro_crew.config import live

        snap = live.snapshot()
        if snap is None:
            return True
        return getattr(getattr(snap, "slack", None), "show_in_session_list", True) is not False
    except Exception:
        return True


def is_slack_born_slot(slot: Any) -> bool:
    """True for a slot surfaced from a conversation that started in Slack."""
    if not getattr(slot, "channel_origin", False):
        return False
    return channel_namespace_of(str(getattr(slot, "key", "") or "")) == "slack"


def slack_row_stamper() -> Callable[[Any, dict[str, Any]], None] | None:
    """The per-row stamp for one slot-list pass, or ``None`` while Slack rows are listed.

    Reads the setting once, so every row of a pass agrees on it.
    """
    if slack_rows_listed():
        return None

    def stamp(slot: Any, row: dict[str, Any]) -> None:
        if is_slack_born_slot(slot):
            row[SLACK_LIST_HIDDEN_KEY] = True

    return stamp
