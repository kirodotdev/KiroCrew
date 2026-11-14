"""The origins enabled, trusted apps add to the dashboard's ``frame-src``.

An app page (``ui.pages``) is an ESM module imported into the dashboard document, so
an iframe it draws is governed by the dashboard's own Content-Security-Policy. An app
names the exact ``https://host`` origins it frames in ``ui.frameSources``. This module
publishes the union of those origins over every app that is installed, enabled and
trusted to execute (:func:`kiro_crew.apps.execution.third_party_ceiling_closed`), as
the string the CSP middleware appends to ``frame-src``.

The scan runs on enable, disable, update and uninstall (``manager_activation``,
``manager_install``, ``manager_uninstall``) and on every hook-reconciler tick. The
reconciler's first scan runs when its task starts, off the boot path; until then no
app origin is admitted. The ticks pick up a change made by the CLI in another
process or a trust grant edited in ``config.json``. The middleware only reads the
published string: no manifest is read per request.

A trusted page module already runs with the dashboard's authority, so admitting the
origins it declares lets it do nothing it could not do before, except show those
sites inside its page.
"""

from __future__ import annotations

import logging
import threading

from kiro_crew.apps.execution import third_party_ceiling_closed

logger = logging.getLogger(__name__)

#: Serializes scan and publication, so an older scan never publishes over a newer one.
_lock = threading.Lock()
_published = ""


def frame_src_extra() -> str:
    """The published origins as a ``frame-src`` suffix (each preceded by a space)."""
    return _published


def _scan() -> list[str]:
    # Through the facade, at call time: the manager owners import this module.
    from kiro_crew.apps.manager import (
        _is_sidecar_entry,
        _read_installed,
        apps_dir,
        get_app_manifest,
    )

    origins: set[str] = set()
    root = apps_dir()
    if not root.is_dir():
        return []
    for entry in root.iterdir():
        name = entry.name
        if _is_sidecar_entry(name) or not entry.is_dir():
            continue
        try:
            meta = _read_installed(name)
            if meta is None or not meta.enabled:
                continue
            manifest = get_app_manifest(name)
            # Re-validated here: app.json is in the app's own writable tree, and an
            # entry reaches a response header as written.
            if manifest is None or not manifest.ui.frameSources:
                continue
            if manifest.ui.frame_source_errors():
                logger.warning("App %s: ignoring invalid ui.frameSources", name)
                continue
            # shortcut: name-only trust check, so a built-in adds nothing here; no
            # built-in declares frameSources and new built-ins are not accepted.
            if third_party_ceiling_closed(name) is not None:
                continue
        except Exception:  # noqa: BLE001 - one damaged app must not drop the others
            logger.warning("App %s: frameSources scan failed", name, exc_info=True)
            continue
        origins.update(manifest.ui.frameSources)
    return sorted(origins)


def refresh_frame_sources() -> str:
    """Rescan installed apps and publish their frame origins. Blocking disk I/O.

    A scan that fails as a whole publishes no origins (fail closed) and logs it.
    """
    global _published
    with _lock:
        try:
            _published = "".join(f" {origin}" for origin in _scan())
        except Exception:  # noqa: BLE001 - fail closed: admit no app origin
            _published = ""
            logger.warning("frameSources scan failed", exc_info=True)
        return _published


__all__ = ["frame_src_extra", "refresh_frame_sources"]
