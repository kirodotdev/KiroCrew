"""Research Lab's providers for core's registration seams.

Core asks for these through :mod:`kiro_crew.apps.seams` and never imports this app.
That module imports this file the first time any seam is looked up.

A provider answers whether or not the app is enabled: a campaign worker's stopped
loop keeps its tombstone with the app switched off too, so a re-enabled app still
reads how its campaigns ended.
"""

from __future__ import annotations

from kiro_crew.apps import seams
from kiro_crew.apps.builtins.auto_research.session_keys import (
    AUTO_RESEARCH_APP,
    is_owned_research_slot,
)


class _StoppedLoopTombstone:
    """:class:`kiro_crew.apps.seams.StoppedLoopTombstone`, from ``session_keys.py``.

    The watchdog reads the tombstone a stopped campaign worker leaves to tell a
    deliberate finish from an unreachable session. Only a canonical worker-slot key
    whose persisted provenance is this app keeps one.
    """

    def keeps_stopped_loop(self, slot_key: str, app_id: str) -> bool:
        return is_owned_research_slot(slot_key, app_id)


seams.register(seams.SEAM_STOPPED_LOOP_TOMBSTONE, AUTO_RESEARCH_APP, _StoppedLoopTombstone())
