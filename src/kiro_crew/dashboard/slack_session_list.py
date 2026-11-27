"""Which rows ``slack.show_in_session_list`` leaves out of the person's session list.

Off (the default), every row of a conversation that STARTED in Slack carries
``slack_list_hidden: True`` (stamped by ``DashboardState.serialize_slots``), and
the dashboard's one person-list filter (``crewModeView`` in
``website/src/lib/crewMode.ts``) leaves it out, the way it leaves out a Crew Mode
worker, except the session open in the main pane and one waiting on the person (a
tool approval or a question), which keep their rows (``slackRowHidden``). Nothing
else changes: the slot stays registered, the conversation keeps
running in Slack, History and search still list it, its approvals and
notifications still reach the person, and turning the setting on drops the
stamp from the next slot list. Nothing is closed, written or deleted.

A Slack-born row is a slot the dashboard surfaced from a ``slack:`` channel
session (``channel_origin`` with a ``slack`` key). A dashboard-born session linked
to a Slack thread, and a Slack code channel's session with its helper agents, are
dashboard sessions with dashboard keys, so they are never stamped and never
filed into the ``Slack`` folder.

The setting is read from the config watcher's last applied snapshot: memory only,
because this runs on every slot-list serialization on the event loop. Only a real
``True`` lists the rows. A missing snapshot (no watcher in this process), a
degraded ``slack`` section (which holds the field default) or any read failure
answers "not listed", the default: an unset key must never list them, and leaving
a row out writes nothing and hides nothing that needs the person. A Settings save
pushes a fresh slot list once the watcher has applied it.

On, with ``slack.session_folder`` empty, a newly surfaced Slack conversation is
filed into a top-level ``Slack`` folder created on first use
(:func:`auto_folder_id`), the zero-config filing Slack code channels get from
their ``Code channels`` folder. A configured ``session_folder`` always wins.

shortcut: a hand edit of ``config.json`` reaches the list on the next slot-list
push, not at once; subscribe a push to the watcher if that lag ever matters.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.channel_folders import configured_folder_name
from kiro_crew.dashboard.chat_folder_order import next_folder_order
from kiro_crew.messaging.link import channel_namespace_of
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Row field the person's list filter drops a row on.
SLACK_LIST_HIDDEN_KEY = "slack_list_hidden"

#: The sidebar folder listed Slack conversations file into when
#: ``slack.session_folder`` is empty. An ASCII literal, like ``Code channels``: a
#: name re-derived from the locale would fight a person's rename of the folder.
AUTO_FOLDER_NAME = "Slack"


def _slack_section() -> Any:
    from kiro_crew.config import live

    return getattr(live.snapshot(), "slack", None)


def slack_rows_listed() -> bool:
    """``slack.show_in_session_list`` as last applied; False unless it is a real True."""
    try:
        return getattr(_slack_section(), "show_in_session_list", False) is True
    except Exception:
        return False


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


def auto_folder_applies() -> bool:
    """Whether new Slack conversations file into :data:`AUTO_FOLDER_NAME`.

    Only while they are listed and ``slack.session_folder`` is empty, read from
    the same applied snapshot as :func:`slack_rows_listed`. Any read failure
    answers no: the conversation lands unfiled, as it did before.
    """
    try:
        section = _slack_section()
        if getattr(section, "show_in_session_list", False) is not True:
            return False
        return not str(getattr(section, "session_folder", "") or "").strip()
    except Exception:
        return False


async def auto_folder_id(state: "DashboardState") -> str:
    """The id of the top-level ``Slack`` folder, created on first use; ``""`` when not used.

    ``""`` when :func:`auto_folder_applies` says no, when ``config.json`` names a
    ``slack.session_folder`` (read off the loop), when two top-level folders
    share the name (filing into an arbitrary one would surprise; the Code channels
    folder answers that case the same way), when the folder cap is reached, or
    when the store write fails. A mate's home folder never answers to the name.

    Find and create run inside one ``mutate_folders`` transaction: the store lock
    is held across the read-modify-write and the ``fsync`` runs off the event
    loop, so two reconcile passes cannot both create the folder. A pass that finds
    the folder writes nothing. A folder the person hid is used as is; the sidebar
    shows a hidden folder again once it holds a live session.
    """
    if not auto_folder_applies():
        return ""
    # Off-loop, from disk: the snapshot can lag a save that just named a folder,
    # and a configured folder (even a missing one) always wins.
    if await asyncio.to_thread(configured_folder_name, "slack"):
        return ""
    # Function-local: both import ``dashboard.state``, which imports this module
    # (through ``state_parts.slots_frame``) at load time.
    from kiro_crew.dashboard.chat_folders_records import MAX_CHAT_FOLDERS
    from kiro_crew.dashboard.mate_folders import is_mate_home

    wanted = AUTO_FOLDER_NAME.casefold()
    created = False
    refused = ""

    def _find_or_create(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        nonlocal created, refused
        matches = [
            f
            for f in folders
            if not str(f.get("parent_id") or "")
            and str(f.get("name") or "").strip().casefold() == wanted
            and not is_mate_home(f)
            and f.get("id")
        ]
        if len(matches) > 1:
            refused = f"{len(matches)} top-level folders are named {AUTO_FOLDER_NAME!r}"
            return False, ""
        if matches:
            return False, str(matches[0]["id"])
        if len(folders) >= MAX_CHAT_FOLDERS:
            refused = "the folder limit is reached"
            return False, ""
        folder = {
            "id": uuid.uuid4().hex[:12],
            "name": AUTO_FOLDER_NAME,
            "order": next_folder_order(folders),
            "collapsed": False,
            "hidden": False,
            "parent_id": "",
            "project_dir": "",
            "created_at": time.time(),
            # No ``channel`` stamp: ``ensure_channel_folder`` takes the stamped
            # folder as the configured ``session_folder``'s own and would relabel
            # this one. Like ``Code channels``, the name is the identity.
        }
        folders.append(folder)
        created = True
        return True, str(folder["id"])

    try:
        folder_id = await state.mutate_folders(_find_or_create)
    except Exception:  # noqa: BLE001 — filing is best-effort; the session lands unfiled
        logger.warning("Slack session folder could not be created", exc_info=True)
        return ""
    if refused:
        logger.info("Slack session folder not used: %s; filing at top level", refused)
        return ""
    if created and folder_id:
        state.push_slots_update()
        sel().log_api_access(
            caller="dashboard",
            operation="chat.channel_folder_create",
            outcome="allowed",
            source="slack",
            resources=folder_id,
        )
        logger.info("Created slack session folder %r (%s)", AUTO_FOLDER_NAME, folder_id)
    return folder_id
