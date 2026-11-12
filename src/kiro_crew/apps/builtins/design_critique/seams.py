"""Design Critique's registrations in the core's :mod:`kiro_crew.apps.seams`.

The core imports this module on its first seam lookup, which ``dashboard.chat_handlers``
makes while it is itself being imported, so it imports nothing of the app. The page's
``openSlot()`` creates its throwaway ``dc-*`` worker slots under the app's own mode, and
repeats it on every send so a slot recreated after a gateway restart keeps it. A
non-``""`` mode is what keeps those slots off the chat sidebar, which lists only plain
``""`` slots.
"""

from __future__ import annotations

from kiro_crew.apps import seams

#: The manifest name, which is also the worker slot mode.
APP_ID = "design-critique"

seams.register_worker_slot_mode(APP_ID)
