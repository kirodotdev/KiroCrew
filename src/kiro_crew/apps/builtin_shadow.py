"""Say when an installed app shares its name with a built-in that still runs.

A folder or git install named like a shipped built-in takes the record, but the
wheel copy keeps serving the name until it is released by adoption
(``builtin_adoption.builtin_released``): its routes register and every hook root
chooser resolves the shipped package. That is a legitimate way to test a local
build, so it is not refused; it is named, at install time and on the app's row.
Owning spec: app-kit-platform.md, "Provenance is immutable at runtime".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.apps.manager import InstalledApp

logger = logging.getLogger(__name__)


def shadowed_builtin_notice(name: str, record: InstalledApp | None) -> str:
    """The sentence naming a still-running built-in that *record* shares a name with, or ``""``."""
    from kiro_crew.apps.builtin_adoption_pins import adoption_pin
    from kiro_crew.apps.execution import shipped_builtin_app_root
    from kiro_crew.apps.manager import _builtin_owns_install

    if record is None or _builtin_owns_install(record):
        return ""
    try:
        shipped_root = shipped_builtin_app_root(name)
        pinned = adoption_pin(name) is not None
    except Exception:  # noqa: BLE001 - the notice is display text; no answer shows nothing
        logger.debug("shadowed built-in check failed", exc_info=True)
        return ""
    if shipped_root is None:
        return ""
    notice = (
        f"the built-in {name} app still runs under this name, so its routes and "
        "hooks run instead of this install's"
    )
    if pinned:
        notice += f"; install {name} from the App Store to replace the built-in"
    return notice
