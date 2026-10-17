"""Crew's overlay on Kiro's ``chat.disableInheritingDefaultResources`` in a workspace ``cli.json``.

The keys it writes, the credential-gated settings read with its retry pause, the
recorded inheritance preference behind :func:`inherits_default_resources`, and the
rollback that restores what the workspace had.

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write, on_event_loop
from kiro_crew.config.paths import kiro_home
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.security import _PATH_RESOLVE_TIMEOUT_SECS

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp.skill_projection")


_MANAGED_SETTING = "kirocrew.skillDiscovery.inheritFiles"
_INHERIT_SETTING = "chat.disableInheritingDefaultResources"
_INHERIT_SOURCE = "kirocrew.skillDiscovery.inheritSource"
_PREVIOUS_INHERITANCE = "kirocrew.skillDiscovery.previousInheritance"


_ABSENT_SETTINGS = object()

#: Pauses before the retries of a refused ``cli.json`` read, in seconds; the
#: first attempt runs before any of them. A healthy file is refused transiently
#: in two ways: a writer renames a new file over the one that was opened (the
#: descriptor check then names the unlinked inode), which clears on an immediate
#: re-check, and a saturated sensitive-path resolver, which clears once a
#: candidate resolution has had its full budget. No other refusal changes with
#: time inside one call, so more pauses would only delay the fail-closed answer.
_SETTINGS_READ_RETRY_PAUSES: tuple[float, ...] = (0.0, _PATH_RESOLVE_TIMEOUT_SECS)

#: The pause primitive of :func:`_settings`, held as a module attribute so a test
#: can replace it without touching the shared ``time`` module.
_settings_read_sleep = time.sleep


def _settings(path: Path, *, may_pause: bool = True) -> dict[str, Any]:
    """Read one Kiro ``cli.json`` through the credential gate; absence reads ``{}``.

    Off the event loop a refused read is retried on
    :data:`_SETTINGS_READ_RETRY_PAUSES`: one immediate re-check, then one pause
    and a final attempt. Every attempt re-runs the whole gate, so a path that
    stays refused still raises. On a thread running an event loop exactly one
    attempt runs, because each attempt is several bounded path resolutions and a
    pause there stalls every session the gateway serves.

    ``may_pause=False`` keeps the immediate re-check but drops the pause, for a
    caller holding a cross-process lock whose contenders give up after the same
    2 s: sleeping there would make a concurrent spawn lose its lock instead.
    """

    def read_once() -> bytes | object | None:
        if not path.exists():
            return _ABSENT_SETTINGS
        return safe_read_file_bytes(str(path))

    raw = read_once()
    if raw is None and not on_event_loop():
        for pause in _SETTINGS_READ_RETRY_PAUSES:
            if pause and not may_pause:
                break
            if pause:
                _settings_read_sleep(pause)
            raw = read_once()
            if raw is not None:
                break
    if raw is None:
        raise ValueError(f"Cannot read Kiro settings at {path}")
    if not isinstance(raw, bytes):
        # The absence sentinel: the file does not exist.
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Kiro settings must be an object: {path}")
    return data


def _inheritance_preference(
    local: dict[str, Any], global_settings: dict[str, Any]
) -> tuple[bool, Any, bool]:
    """Return ``(inherited, source, overlaid)`` for custom agents in one workspace.

    Once Crew's overlay is in place the native key reads ``true`` whatever the
    user chose, so the recorded preference decides: a ``global`` source follows
    the live global setting and any other source keeps the recorded value.
    Without the overlay, or after the user moves the native key off ``true``,
    the native key decides -- the workspace value when present, else the global
    one -- and only the literal ``true`` opts out.
    """
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or local.get(_INHERIT_SETTING) is not True:
        source = "local" if _INHERIT_SETTING in local else "global"
        native = local.get(_INHERIT_SETTING, global_settings.get(_INHERIT_SETTING))
        return native is not True, source, False
    if source == "global":
        return global_settings.get(_INHERIT_SETTING) is not True, source, True
    return inherited, source, True


def inherits_default_resources(work_dir: str | os.PathLike[str] | None) -> bool:
    """Whether a custom agent started in *work_dir* inherits kiro-cli's default resources.

    Those defaults are global and workspace steering, ``AGENTS.md`` and the
    global and workspace skill trees. This is the read-only twin of the decision
    :func:`prepare_native_skill_projection` makes, for callers that must mirror
    what the native agent loads. Settings
    that cannot be read keep inheritance, which is how those callers behaved
    before they asked.
    """
    try:
        global_settings = _settings(kiro_home() / "settings" / "cli.json")
        local = (
            _settings(Path(work_dir) / ".kiro" / "settings" / "cli.json")
            if work_dir is not None
            else {}
        )
    # RuntimeError: Path.resolve() reports a symlink loop that way before Python 3.13.
    except (OSError, ValueError, RuntimeError, RecursionError, FileTooLargeError):
        logger.warning(
            "skill projection: Kiro settings unreadable; keeping inherited resources",
            exc_info=True,
        )
        return True
    return _inheritance_preference(local, global_settings)[0]


def _restore_inheritance(path: Path, local: dict[str, Any]) -> None:
    """Undo only our overlay; a changed or removed native setting wins."""
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or source not in ("local", "global"):
        return
    previous = local.get(_PREVIOUS_INHERITANCE)
    if previous is None:
        # Views prepared before rollback support recorded source and a boolean.
        previous = {"present": source == "local", "value": not inherited}
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("present"), bool)
        or (previous["present"] and "value" not in previous)
    ):
        raise ValueError(f"Cannot restore Crew's inheritance overlay at {path}")
    if local.get(_INHERIT_SETTING) is True:
        if previous["present"]:
            local[_INHERIT_SETTING] = previous["value"]
        else:
            local.pop(_INHERIT_SETTING, None)
    for key in (_MANAGED_SETTING, _INHERIT_SOURCE, _PREVIOUS_INHERITANCE):
        local.pop(key, None)
    atomic_write(path, json.dumps(local, indent=2))
