"""An agent's ``welcomeMessage`` on the dashboard, before and after the first message.

An unstarted chat holds no transcript row. A pick on an empty chat is no
boundary (``chat_api.agent_switch._append_session_switch_notice``), and a row
there would replace the welcome page, stack one greeting per agent tried in the
picker, and count as history for the member-memory gates. So until the first
message the greeting rides the slot summary instead: the slot create, the picker
commit and the workspace and project switches read it here
(:func:`refresh_welcome_preview`), the summary ships it as ``welcome_message``
(:func:`welcome_preview`), and the welcome page draws it.

From the first row on the summary ships ``""``, and the row the first turn's cold
start writes (``chat_turn.turn_spec_hooks._surface_agent_welcome``) carries the
greeting. That row is tagged ``meta.kind == AGENT_WELCOME_KIND``, which is what
keeps it out of the turn fold (``TurnBlock.isAlwaysVisible`` in the website).

Both halves read through the runner's ``agent_welcome_message`` and apply the
display redactors ``_redact_display_text`` applies, so the page and the row show
the same text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.agent_welcome")

#: The ``meta["kind"]`` of the welcome transcript row. A WIRE VALUE: the website
#: matches it byte for byte (``TurnBlock.isAlwaysVisible``), pinned by
#: ``test/test_agent_welcome_message.py``. Not one of
#: ``system_notices.SYSTEM_NOTICE_KINDS``: that set marks rows the "last real
#: message" scans skip, and the greeting is meant to be read.
AGENT_WELCOME_KIND = "agent_welcome"

#: ``(agent, agent_kind, project, text)`` with nothing read.
_NO_PREVIEW: tuple[str, str, str, str] = ("", "", "", "")


def _binding(slot: Any) -> tuple[str, str, str]:
    """What the greeting depends on: the picked agent, its namespace, the project."""
    return (slot.agent or "", getattr(slot, "agent_kind", "") or "", slot.project or "")


def welcome_preview(slot: Any) -> str:
    """The greeting the welcome page shows for *slot*, or ``""``. No I/O.

    Only a reading taken for the slot's CURRENT binding counts. A path that
    changes the agent, its namespace or the project without a refresh leaves a
    stale entry, and that reads as no greeting rather than another agent's.
    """
    preview = getattr(slot, "_welcome_preview", None) or _NO_PREVIEW
    return preview[3] if preview[:3] == _binding(slot) else ""


def _has_rows(slot: Any) -> bool:
    """Whether *slot* holds any row, without loading a pending window."""
    if getattr(slot, "window_pending", False) is True:
        return bool(slot.sidebar_messages())
    return bool(slot.messages)


def _display_text(text: str) -> str:
    """*text* through the redactors the welcome row passes through.

    ``chat_turn.deny_chokepoints._redact_display_text`` applies the same two, in
    this order, to the row the cold start writes.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _read_preview(binding: tuple[str, str, str], read: Callable[..., str]) -> str:
    """Blocking: the display-ready greeting of the agent that will answer, or ``""``.

    That agent is the one the first turn's cold start greets with
    (``kiro_agent or slot.agent``): a crewmate pick resolves to its template,
    whose spec carries the field, because the crewmate's own name names none. A
    name the agent snapshot does not resolve yet is read as itself, which is how
    the turn's rescan finds a spec that is on disk.
    """
    from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings

    agent, agent_kind, project = binding
    chosen = resolve_agent_bindings(
        KiroCrewConfig.load(),
        agent or None,
        project or None,
        validate_memory_files=False,
        selection_kind=agent_kind,
    )
    name = (chosen.kiro_agent if chosen.requested_resolved else "") or agent
    text = read(name, project=project or None)
    return _display_text(text) if text else ""


async def refresh_welcome_preview(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Read the greeting for *slot*'s current binding and publish it in the summary.

    Called where the binding of a chat that may not have started is set: the
    slot create, the agent switch, and the workspace and project switches,
    since a project's own spec can shadow the user-level one. A chat that already
    has a row reads nothing, since its summary ships no greeting. Best-effort and
    total: a read that fails publishes no greeting, and a binding that changed
    during the read keeps nothing, since the change that moved it reads its own.
    Pushes the slots update only when the greeting the summary shows changes: a
    reading that moves only the binding, such as a workspace switch on the
    default crew, changes nothing on the page, and each switch pushes its own
    update after this returns.
    """
    if _has_rows(slot):
        return
    # The runner's reader, the one the row is read through, so the page shows
    # what the row will.
    from kiro_crew.dashboard import chat_runner

    binding = _binding(slot)
    try:
        text = await asyncio.to_thread(_read_preview, binding, chat_runner.agent_welcome_message)
    except Exception:  # noqa: BLE001 - a greeting must never fail a create or a pick
        logger.debug("Failed to read welcomeMessage for slot %s", slot.key, exc_info=True)
        text = ""
    if state._slots.get(slot.key) is not slot or _binding(slot) != binding:
        return
    shown = welcome_preview(slot)
    slot._welcome_preview = (*binding, text)
    if text != shown:
        state.push_slots_update()
