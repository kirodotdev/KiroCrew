"""Which apps have a public id: the shipped built-ins and the apps installed from the store.

An app's public id is its name when that name is already public, so sending it
discloses nothing the operator chose. Two kinds of app qualify:

* a built-in the package ships, by the ``name`` in its ``app.json``
  (:func:`kiro_crew.apps.discovery.discover_builtin_apps`; an edition's own
  built-ins are not included);
* an app installed from the public store. Its install record says
  ``origin=registry``, a ``registry:<name>`` source, a non-empty ``sourceUrl``, an
  empty ``sourceRegistry`` and ``dev`` off (a dev-mode app serves edited code).
  ``install_from_registry`` writes exactly that for a row with no ``_registry``
  marker, the rows it also sends an install receipt for. A row from an
  owner-configured registry records that registry's id, so it is private.

The answer comes from the install record only, never from today's catalog, so it is
fixed when the app is installed: an app the store later delists keeps its id. A
reinstall from anywhere else rewrites the record, and that install is private.

shortcut: ``installed.json`` is writable from the app's own sandbox (see
:mod:`kiro_crew.apps.protected_app_state`), so an app's code can make its own record
look like a store install and have its own directory name sent. It can only name
itself, a name its code already holds. Move the origin into a ``member-records`` kind
if a count ever has to resist that.
"""

from __future__ import annotations

import asyncio
import functools
import logging

logger = logging.getLogger(__name__)


@functools.cache
def _shipped_builtin_ids() -> frozenset[str]:
    """The ``app.json`` names of the built-ins in this package. Read once per process."""
    from kiro_crew.apps.discovery import discover_builtin_apps

    return frozenset(
        app["name"] for app in discover_builtin_apps() if isinstance(app.get("name"), str)
    )


def public_app_ids() -> frozenset[str]:
    """Every app on this machine with a public id. Reads the install records: call off the loop."""
    from kiro_crew.apps.manager import (
        InstalledRecordShapeError,
        _is_sidecar_entry,
        _read_installed,
        apps_dir,
    )
    from kiro_crew.apps.update_status import _recorded_source

    ids = set(_shipped_builtin_ids())
    root = apps_dir()
    if not root.is_dir():
        return frozenset(ids)
    for entry in root.iterdir():
        if _is_sidecar_entry(entry.name) or not entry.is_dir():
            continue
        try:
            meta = _read_installed(entry.name)
        except (InstalledRecordShapeError, OSError, ValueError) as exc:
            logger.debug("public app ids: skipping %s: %s", entry.name, exc)
            continue
        if meta is None or meta.origin != "registry" or meta.dev:
            continue
        # The update check's own reading of a registry record: (sourceUrl, sourceRegistry).
        recorded = _recorded_source(
            {
                "source": meta.source,
                "sourceUrl": meta.sourceUrl,
                "sourceRegistry": meta.sourceRegistry,
            }
        )
        if recorded is not None and recorded[1] == "":
            ids.add(entry.name)
    return frozenset(ids)


async def count_first_install(name: str, via: str) -> None:
    """Count *name*'s first registry install, after its provenance is on disk.

    The public ids are re-read first, off the loop, so the count names an app this
    install just made public. Never raises past a cancellation.
    """
    from kiro_crew.metrics.feature_usage import record_app_installed, refresh_public_app_ids

    await asyncio.to_thread(refresh_public_app_ids)
    record_app_installed(name, via)
