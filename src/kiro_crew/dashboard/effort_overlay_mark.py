"""Carry a slot's recorded Default to its next spawn, and drop the mark once it lands.

kiro-cli reads its effort from the workspace ``cli.json`` at spawn. The effort
route clears a model's level there live (``clear_effort``) when it can; when it
records Default with no live clear it sets ``slot._effort_overlay_clear``
instead (``chat_api.slot_effort``). The turn and the eager spawn hand that mark
to ``get_or_create`` as ``clear_effort_overlay``, and the provider's first start
removes the level (``AcpProvider._clear_default_overlay_entry``).
"""

from __future__ import annotations

from typing import Any


def set_effort_overlay_clear_mark(slot: Any, value: bool) -> None:
    """Record whether *slot*'s next spawn must clear its model's workspace effort.

    The effort route sets it for a Default committed with no live clear and drops
    it for every other commit. Saved by the periodic flush when it changes.
    """
    if slot._effort_overlay_clear != value:
        slot._effort_overlay_clear = value
        slot._dirty = True


def effort_overlay_clear_kwargs(slot: Any) -> dict[str, bool]:
    """``{"clear_effort_overlay": True}`` while *slot* carries the mark, else ``{}``."""
    return {"clear_effort_overlay": True} if slot._effort_overlay_clear else {}


def settle_effort_overlay_clear(
    slot: Any, carried: dict[str, bool], *, is_new: bool, provider: Any
) -> None:
    """Drop *slot*'s mark once a spawn that carried it removed the level.

    A claim of a session that was already running spawned nothing, and a start
    whose clear the overlay lock refused still reports it pending, so the mark
    stays for the next spawn in both cases.
    """
    if (
        not carried
        or not is_new
        or getattr(provider, "effort_overlay_clear_pending", False) is True
    ):
        return
    slot._effort_overlay_clear = False
    slot._dirty = True
