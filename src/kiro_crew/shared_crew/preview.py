"""The Shared Crew Feature preview switch.

Until the owner turns Shared Crew on, nothing in it may be switched on from
config by accident. With the switch off (the default) every
``dashboard.shared_crew`` switch reads as its default-off value whatever
config.json says, so the gateway behaves as it did before Shared Crew.

Two things turn it on, either one enough:

* ``KIROCREW_SHARED_CREW_PREVIEW=1`` in the gateway's environment (exactly
  ``1``);
* the owner's stored choice in the ``shared_crew_preview.json`` keystone,
  written only by the owner-only ``PUT /api/shared-crew/preview``
  (:mod:`kiro_crew.shared_crew.routes_preview`). No config field, Settings row
  or change card can turn it on, and an in-sandbox agent cannot write the file.

The stored choice is read once, when the gateway starts
(:func:`snapshot_at_start`), and :func:`preview_enabled` answers from that
snapshot for the life of the process. Routes, the direct listener and the
lifecycle hooks are wired at start, so a changed choice applies on the next
start, and the switch reports ``restart_required`` until then.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.atomic_write import rewrite_in_place

logger = logging.getLogger(__name__)

#: The gateway environment variable that opts this host into the preview.
PREVIEW_ENV = "KIROCREW_SHARED_CREW_PREVIEW"

_LOCK = threading.Lock()
#: The stored choice as read at gateway start; ``None`` until then.
_boot_stored: bool | None = None


def env_override() -> bool:
    """Whether the environment forces the preview on. Exactly ``1`` does."""
    return os.environ.get(PREVIEW_ENV, "") == "1"


def _path() -> Any:
    # Deferred: this module is imported on the gateway boot path, which must
    # not pull ``config.loader`` in with it.
    from kiro_crew.config.loader import shared_crew_preview_path

    return shared_crew_preview_path()


def read_stored() -> bool:
    """The owner's stored choice. Only a JSON ``true`` is on; anything else is off."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 -- an unreadable record reads as the default
        logger.debug("shared_crew_preview.json unreadable; reading as off", exc_info=True)
        return False
    return isinstance(raw, dict) and raw.get("enabled") is True


#: Serializes the owner's writes; the route is the only writer.
_WRITE_LOCK = threading.Lock()


def write_stored(enabled: bool) -> None:
    """Persist the owner's choice. Does not change the running process's answer.

    The record is rewritten in place (:func:`atomic_write.rewrite_in_place`), so a
    running Linux sandbox's read-only mount of it stays sealed. A torn read during
    the rewrite reads as off, the narrow answer.
    """
    data = json.dumps({"enabled": bool(enabled)}, indent=2, sort_keys=True).encode("utf-8")
    with _WRITE_LOCK:
        rewrite_in_place(_path(), data)


def snapshot_at_start() -> bool:
    """Read the stored choice once and keep it for this process; later calls return it."""
    global _boot_stored
    with _LOCK:
        if _boot_stored is None:
            _boot_stored = read_stored()
        return _boot_stored


def preview_enabled() -> bool:
    """Whether this process runs the Shared Crew preview.

    The environment override, or the stored choice as it was when this process
    started.
    """
    return env_override() or snapshot_at_start()


@dataclass(frozen=True)
class PreviewSetting:
    """The switch as the owner sees it."""

    #: The stored choice (what the next start will run, absent the override).
    enabled: bool
    #: What this process started with.
    active: bool
    #: The environment forces it on, whatever is stored.
    env_override: bool
    #: The next start will answer differently from this one.
    restart_required: bool

    def to_dict(self) -> dict[str, bool]:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "env_override": self.env_override,
            "restart_required": self.restart_required,
        }


def current_setting() -> PreviewSetting:
    """Read the stored choice and compare it with what this process runs."""
    stored = read_stored()
    forced = env_override()
    active = preview_enabled()
    return PreviewSetting(
        enabled=stored,
        active=active,
        env_override=forced,
        restart_required=(forced or stored) != active,
    )


def reset_for_tests() -> None:
    """Forget the boot snapshot so the next read takes a fresh one."""
    global _boot_stored
    with _LOCK:
        _boot_stored = None


def preview_only(handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
    """404 while the Shared Crew preview is off.

    The route answers as if it did not exist, so nothing here can be used, or
    turned on, by accident before the owner ships Shared Crew. It lives here,
    not with the invite routes, so a boot-path module can gate one route
    without importing a preview-only module.
    """

    @functools.wraps(handler)
    async def gated(request: Any) -> Any:
        if not preview_enabled():
            from aiohttp import web

            raise web.HTTPNotFound()
        return await handler(request)

    return gated


def no_maintenance() -> None:
    """The post-bind maintenance starter a server uses while the preview is off."""
    return None


__all__ = [
    "PREVIEW_ENV",
    "PreviewSetting",
    "current_setting",
    "env_override",
    "no_maintenance",
    "preview_enabled",
    "preview_only",
    "read_stored",
    "reset_for_tests",
    "snapshot_at_start",
    "write_stored",
]
