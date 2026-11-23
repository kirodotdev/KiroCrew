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

:class:`PreviewSwitch`, :meth:`PreviewSetting.of` and :func:`gate` are the
mechanism every owner's preview switch of this shape runs through; the memory
provider switch (:mod:`kiro_crew.memory_provider.preview`) binds them too.
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


class PreviewSwitch:
    """One preview switch: an environment override and a keystone read once at start."""

    def __init__(self, env: str, path: Callable[[], Any]) -> None:
        self._env = env
        self._path = path
        self._lock = threading.Lock()
        #: Serializes the owner's writes; the switch's route is the only writer.
        self._write_lock = threading.Lock()
        #: The stored choice as read at gateway start; ``None`` until then.
        self._boot_stored: bool | None = None

    def env_override(self) -> bool:
        return os.environ.get(self._env, "") == "1"

    def read_stored(self) -> bool:
        path = self._path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001 -- an unreadable record reads as the default
            logger.debug("%s unreadable; reading as off", path.name, exc_info=True)
            return False
        return isinstance(raw, dict) and raw.get("enabled") is True

    def write_stored(self, enabled: bool) -> None:
        data = json.dumps({"enabled": bool(enabled)}, indent=2, sort_keys=True).encode("utf-8")
        with self._write_lock:
            rewrite_in_place(self._path(), data)

    def snapshot_at_start(self) -> bool:
        with self._lock:
            if self._boot_stored is None:
                self._boot_stored = self.read_stored()
            return self._boot_stored

    def reset_for_tests(self) -> None:
        with self._lock:
            self._boot_stored = None


def _path() -> Any:
    # Deferred: this module is imported on the gateway boot path, which must
    # not pull ``config.loader`` in with it.
    from kiro_crew.config.loader import shared_crew_preview_path

    return shared_crew_preview_path()


_SWITCH = PreviewSwitch(PREVIEW_ENV, _path)


def env_override() -> bool:
    """Whether the environment forces the preview on. Exactly ``1`` does."""
    return _SWITCH.env_override()


def read_stored() -> bool:
    """The owner's stored choice. Only a JSON ``true`` is on; anything else is off."""
    return _SWITCH.read_stored()


def write_stored(enabled: bool) -> None:
    """Persist the owner's choice. Does not change the running process's answer.

    The record is rewritten in place (:func:`atomic_write.rewrite_in_place`), so a
    running Linux sandbox's read-only mount of it stays sealed. A torn read during
    the rewrite reads as off, the narrow answer.
    """
    _SWITCH.write_stored(enabled)


def snapshot_at_start() -> bool:
    """Read the stored choice once and keep it for this process; later calls return it."""
    return _SWITCH.snapshot_at_start()


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

    @classmethod
    def of(cls, stored: bool, forced: bool, active: bool) -> PreviewSetting:
        """The setting for a stored choice, an override and what this process runs."""
        return cls(
            enabled=stored,
            active=active,
            env_override=forced,
            restart_required=(forced or stored) != active,
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "env_override": self.env_override,
            "restart_required": self.restart_required,
        }


def current_setting() -> PreviewSetting:
    """Read the stored choice and compare it with what this process runs."""
    return PreviewSetting.of(read_stored(), env_override(), preview_enabled())


def reset_for_tests() -> None:
    """Forget the boot snapshot so the next read takes a fresh one."""
    _SWITCH.reset_for_tests()


def gate(
    enabled: Callable[[], bool], handler: Callable[[Any], Awaitable[Any]]
) -> Callable[[Any], Awaitable[Any]]:
    """Wrap *handler* to answer 404 while *enabled* is false, asked per request."""

    @functools.wraps(handler)
    async def gated(request: Any) -> Any:
        if not enabled():
            from aiohttp import web

            raise web.HTTPNotFound()
        return await handler(request)

    return gated


def preview_only(handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
    """404 while the Shared Crew preview is off.

    The route answers as if it did not exist, so nothing here can be used, or
    turned on, by accident before the owner ships Shared Crew. It lives here,
    not with the invite routes, so a boot-path module can gate one route
    without importing a preview-only module.
    """
    return gate(lambda: preview_enabled(), handler)


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
