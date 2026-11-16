"""When a chat takes the title its harness gave the session: only in place of Crew's echo.

Crew titles a chat itself (``chat_title._maybe_auto_title``). When its model names
nothing (a SKIP, a failed call, the attempts spent), the chat keeps the echo of its
first message (``_fallback_title_from_messages``). On a backend in
``ACP_BACKENDS_SESSION_TITLE`` the harness has usually named the session by then
(``acp.harness_title``), so once chat_done's whole title chain has run
(:func:`titled_then_harness`), :func:`adopt_harness_title` puts that title where the
echo was. Crew stays the title's owner: a title its model wrote, a rename, a pinned
or crewmate title, a fork's name and a slot Crew does not auto-title are all kept,
as is a session holding a turn an app sent or a restored turn, since the harness
titled from whatever was sent first. The harness's own echo of Crew's prompt (its
summary fallback, cut with an ellipsis) and what Crew's reply validator refuses are
not taken.

The adopted title is an ``auto`` title like the one Crew's model writes, so the
milestone refresh may still rename it in the UI language.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable

from kiro_crew.agent_sdk.drivers.acp_vocab import harness_session_title
from kiro_crew.dashboard.chat_folder_suggest import maybe_suggest_folder
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AUTO,
    FORK_TITLE_MARKER,
    _counts_as_user_turn,
    _current_ticket_key,
    _fallback_title_from_messages,
    _is_low_signal_title,
    _is_member_thread,
    _persist_title,
    _split_ticket_prefix,
    _ticket_key_if_enabled,
    _titling_messages,
    _validate_title_reply,
    _with_ticket_prefix,
)
from kiro_crew.dashboard.chat_title_fork import fork_title_pending
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.mrkdwn_title import is_slack_slot

logger = logging.getLogger(__name__)

#: The longest harness title taken, the bound Crew's own titles have
#: (``_validate_title_reply``). A longer one is refused, not cut.
_MAX_ADOPTED_CHARS = 80
#: How the harness ends a title it cut: its summary fallback, Crew's whole prompt.
_HARNESS_CUT = "…"


async def titled_then_harness(
    state: DashboardState, slot: _ChatSlot, titling: Awaitable[None]
) -> None:
    """Run chat_done's title chain (*titling*), then offer the harness's title."""
    await titling
    try:
        await adopt_harness_title(state, slot)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the harness's title is an extra, never a failure
        logger.debug("harness title adoption failed for slot %s", slot.key, exc_info=True)


def _may_take(state: DashboardState, slot: _ChatSlot) -> bool:
    if state._slots.get(slot.key) is not slot or _is_member_thread(slot):
        return False
    if slot._title_in_flight or slot.title.startswith(FORK_TITLE_MARKER):
        return False
    if fork_title_pending(slot):
        return False
    return all(_counts_as_user_turn(slot, m) for m in slot.messages if m.get("role") == "user")


def _echo(slot: _ChatSlot) -> str:
    """The echo of the first message when it is *slot*'s auto title, else ``""``."""
    if slot._title_origin != _TITLE_ORIGIN_AUTO:
        return ""
    fallback = _fallback_title_from_messages(_titling_messages(slot), slack=is_slack_slot(slot))
    return fallback if _split_ticket_prefix(slot.title)[1] == fallback else ""


def _folded(text: str) -> str:
    return " ".join(text.split()).casefold()


def _candidate(raw: str, echo: str, slot: _ChatSlot) -> str:
    """*raw* as a title Crew would have written, or ``""`` when it is refused."""
    if len(raw) > _MAX_ADOPTED_CHARS or raw.endswith(_HARNESS_CUT):
        return ""
    title = _validate_title_reply(raw)
    if not title:
        return ""
    mine, theirs = _folded(title), _folded(echo.removesuffix(_HARNESS_CUT))
    if mine.startswith(theirs) or theirs.startswith(mine):
        return ""
    if _is_low_signal_title(title, _titling_messages(slot), slack=is_slack_slot(slot)):
        return ""
    return title


def _offered(state: DashboardState, slot: _ChatSlot) -> str:
    """The title the harness gave *slot*'s live session; peeked, never started."""
    key = slot._active_turn_session_key or effective_session_key(slot)
    try:
        return harness_session_title(state.sessions.get_provider(key))
    except Exception:  # noqa: BLE001 - no live session to read is no title
        logger.debug("no harness title readable for slot %s", slot.key, exc_info=True)
        return ""


async def adopt_harness_title(state: DashboardState, slot: _ChatSlot) -> bool:
    """Put the harness's title where *slot* shows Crew's echo; whether it took it."""
    if not _may_take(state, slot):
        return False
    raw = _offered(state, slot)
    echo = _echo(slot) if raw else ""
    title = _candidate(raw, echo, slot) if echo else ""
    if not title:
        return False
    epoch = slot._title_epoch
    key = await _ticket_key_if_enabled(_current_ticket_key(slot.title, _titling_messages(slot)))
    # A rename bumps the epoch synchronously, and a send may have started a title
    # attempt during the config hop: either outranks this title.
    if slot._title_epoch != epoch or not _may_take(state, slot) or _echo(slot) != echo:
        return False
    was_titled = slot._titled
    slot.title = _with_ticket_prefix(title, key)
    slot._titled = True
    slot._title_origin = _TITLE_ORIGIN_AUTO
    slot._title_low_signal = False
    slot._title_epoch += 1
    epoch = slot._title_epoch

    def still_current() -> bool:
        return state._slots.get(slot.key) is slot

    await _persist_title(state, slot, still_current=still_current)
    if slot._title_epoch != epoch or not still_current():
        return True
    state.push_slot_title(slot.key, slot.title)
    logger.info("Slot %s took its harness's title in place of its echo", slot.key)
    if not was_titled:
        try:
            await maybe_suggest_folder(state, slot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let a suggestion break titling
            logger.debug("Folder suggestion failed for slot %s", slot.key, exc_info=True)
    return True
