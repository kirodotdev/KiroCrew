"""Which dashboard session is Mastermind, whether Mastermind is on, and the
turn-end duty.

A leaf the dashboard runner reads on every turn, so it never loads the tree
(``service.py``), which only a Mastermind turn needs.

Mastermind is on when ``<data home>/mastermind/enabled`` exists; the answer is
read once per gateway and kept on its state, and only the settings route
(``set_enabled``) changes it, so a turn never touches the disk to ask.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew.mastermind.constants import ENABLED_FILE, MASTERMIND_DIR_NAME

#: The dashboard slot that runs as Mastermind.
MASTERMIND_SLOT_KEY = "mastermind"

_ENABLED_ATTR = "_mastermind_enabled"
SERVICE_ATTR = "_mastermind_service"


def tree_dir() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / MASTERMIND_DIR_NAME


def enabled(state: Any) -> bool:
    cached = getattr(state, _ENABLED_ATTR, None)
    if cached is None:
        cached = (tree_dir() / ENABLED_FILE).exists()
        setattr(state, _ENABLED_ATTR, cached)
    return bool(cached)


def set_enabled(state: Any, on: bool) -> None:
    """Write the switch (blocking: call off the event loop) and keep it on *state*."""
    marker = tree_dir() / ENABLED_FILE
    if on:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    else:
        marker.unlink(missing_ok=True)
    setattr(state, _ENABLED_ATTR, on)


def is_mastermind_slot(slot: Any) -> bool:
    return getattr(slot, "key", None) == MASTERMIND_SLOT_KEY


def is_mastermind_turn(state: Any, slot: Any) -> bool:
    """Whether a turn of *slot* runs as Mastermind: the slot, with Mastermind on."""
    return is_mastermind_slot(slot) and enabled(state)


def turn_ended(state: Any, slot: Any, session_key: str) -> None:
    """At a Mastermind turn's end: if it landed, arm the discard of the session's
    backend conversation for this turn boundary, so the next turn starts fresh and
    the memory view is its only memory. A turn that did not land (stopped, or
    about to be continued by a recovery) keeps its conversation for its
    continuation."""
    service = getattr(state, SERVICE_ATTR, None)
    if service is not None and service.end_turn(slot.key):
        slot._pending_discard_conversation_key = session_key
