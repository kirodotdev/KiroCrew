"""The memory provider Feature preview switch.

Until the owner turns it on, no part of the memory provider feature can be
reached. With the switch off (the default) the configured ``memory.provider``
and every per-store ``provider_mode`` read as Crew's own memory
(:func:`kiro_crew.memory_provider.resolver._chosen`), every
``/api/memory/provider`` route answers 404, and ``permissions.memoryAdmin`` is
neither granted nor honoured.

Two things turn it on, either one enough:

* ``KIROCREW_MEMORY_PROVIDER_PREVIEW=1`` in the gateway's environment (exactly
  ``1``);
* the owner's stored choice in the ``memory_provider_preview.json`` keystone,
  written only by the owner-only ``PUT /api/memory/provider-preview``
  (:mod:`kiro_crew.memory_provider.routes_preview`). No config field can turn it
  on, and an in-sandbox agent cannot write the file.

The stored choice is read once, when the gateway starts
(:func:`snapshot_at_start`), and :func:`preview_enabled` answers from that
snapshot for the life of the process, so a changed choice applies on the next
start and the switch reports ``restart_required`` until then. The mechanism is
the Shared Crew switch's (:class:`kiro_crew.shared_crew.preview.PreviewSwitch`),
bound here to this switch's variable and keystone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.shared_crew.preview import PreviewSetting, PreviewSwitch, gate

#: The gateway environment variable that opts this host into the preview.
PREVIEW_ENV = "KIROCREW_MEMORY_PROVIDER_PREVIEW"


def _path() -> Any:
    from kiro_crew.config.loader import memory_provider_preview_path

    return memory_provider_preview_path()


_SWITCH = PreviewSwitch(PREVIEW_ENV, _path)
env_override = _SWITCH.env_override
read_stored = _SWITCH.read_stored
#: Persists the owner's choice in place; the running process's answer stays.
write_stored = _SWITCH.write_stored
snapshot_at_start = _SWITCH.snapshot_at_start
reset_for_tests = _SWITCH.reset_for_tests


def preview_enabled() -> bool:
    """The environment override, or the stored choice as it was when this process started."""
    return env_override() or snapshot_at_start()


def current_setting() -> PreviewSetting:
    """Read the stored choice and compare it with what this process runs."""
    return PreviewSetting.of(read_stored(), env_override(), preview_enabled())


def preview_only(handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
    """404 while the memory provider preview is off, checked when the request arrives."""
    return gate(lambda: preview_enabled(), handler)


__all__ = [
    "PREVIEW_ENV",
    "current_setting",
    "env_override",
    "preview_enabled",
    "preview_only",
    "read_stored",
    "reset_for_tests",
    "snapshot_at_start",
    "write_stored",
]
