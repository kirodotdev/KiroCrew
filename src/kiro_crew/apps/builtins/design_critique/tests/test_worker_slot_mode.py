"""Design Critique's ``dc-*`` worker slots are created under the app's own mode.

The page's ``openSlot()`` posts ``mode: "design-critique"`` and repeats it on every
send. The core accepts it only because this app registers it from its ``seams.py``
(through ``kiro_crew.apps.seams``); a slot created without it would be a plain ``""``
chat and land in the user's chat sidebar.
"""

from __future__ import annotations

from kiro_crew.apps import seams
from kiro_crew.dashboard import chat_handlers


def test_the_app_registers_its_own_name_as_its_worker_mode() -> None:
    assert "design-critique" in seams.worker_slot_modes()


def test_the_chat_create_paths_accept_it() -> None:
    assert "design-critique" in chat_handlers._CREATABLE_MODES
    assert chat_handlers._CREATABLE_MODES[0] == ""
