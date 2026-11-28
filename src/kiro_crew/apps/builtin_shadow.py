"""Say when an installed app shares its name with a built-in that still runs.

A folder or git install named like a shipped built-in takes the record. When the
name is pinned or marked for adoption, that record releases the wheel copy
(``builtin_adoption.builtin_released``), so the install's own routes and hooks
run and no notice is needed. When the name is neither pinned nor marked, the
wheel copy keeps serving it: its routes register and every hook root chooser
resolves the shipped package, so the install's own code does not run. That is a
legitimate way to test a local build, so it is not refused; it is named, at
install time and on the app's row. Owning spec: app-kit-platform.md, "Provenance
is immutable at runtime".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.apps.manager import InstalledApp

logger = logging.getLogger(__name__)


def shadowed_builtin_notice(name: str, record: InstalledApp | None) -> str:
    """The sentence naming a still-running built-in that *record* shares a name with, or ``""``.

    Empty once the wheel copy is released (a pinned or marked name, where the
    install's own code runs): ``shipped_builtin_app_root`` answers ``None`` then.
    """
    from kiro_crew.apps.execution import shipped_builtin_app_root
    from kiro_crew.apps.manager import _builtin_owns_install

    if record is None or _builtin_owns_install(record):
        return ""
    try:
        shipped_root = shipped_builtin_app_root(name)
    except Exception:  # noqa: BLE001 - the notice is display text; no answer shows nothing
        logger.debug("shadowed built-in check failed", exc_info=True)
        return ""
    if shipped_root is None:
        return ""
    return (
        f"the built-in {name} app still runs under this name, so its routes and "
        "hooks run instead of this install's"
    )
