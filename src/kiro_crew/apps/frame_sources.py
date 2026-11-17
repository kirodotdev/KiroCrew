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

Each origin is an entry of the :data:`GRANT_FAMILY` family in the app's approved-grants
set, staged like a link-preview host (``point_grants``): an install approves what it
declares, and an origin an update or a later edit of ``app.json`` adds is not published
until the owner approves it on the app's page. The family is explicit-only, so a set
stored before this host knew the key approves no origin.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from kiro_crew.apps.execution import third_party_ceiling_closed

logger = logging.getLogger(__name__)

#: The manifest key under ``ui``, and the grant family that stores it.
GRANT_FAMILY = "frameSources"
FRAME_SOURCE_GRANT_FAMILIES = (GRANT_FAMILY,)


def _declared(ui: Any) -> list[str]:
    """The string entries of a manifest's ``ui.frameSources`` (parsed or a raw dict)."""
    value = ui.get(GRANT_FAMILY) if isinstance(ui, dict) else getattr(ui, GRANT_FAMILY, None)
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(e for e in value if isinstance(e, str) and e))


def frame_source_grant_entries(ui: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest's ``ui`` declares."""
    return {GRANT_FAMILY: _declared(ui)}


def _approved(app: str, declared: list[str]) -> list[str]:
    """The *declared* origins in *app*'s approved-grants set, read fresh."""
    if not declared:
        return []
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    approved = set(staged_app_grants(app, GRANT_FAMILY, lambda: declared))
    return [origin for origin in declared if origin in approved]


def frame_sources_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``frameSourcesGrant`` key of an app row, absent when nothing is declared.

    ``approved`` lists the declared origins the owner's approved set holds, which is
    all ``frame-src`` may carry; ``staged`` the declared ones it does not, which the
    detail page offers for approval. Nothing is staged while the declaration fails the
    validation the scan applies (``UIConfig.frame_source_errors``): the scan then
    publishes none of the app's origins, so an approval could not take effect.
    """
    # Deferred like the manager owners: this module loads at call time.
    from kiro_crew.apps.manifest_sections import UIConfig  # noqa: PLC0415

    ui = manifest.get("ui") if isinstance(manifest, dict) else getattr(manifest, "ui", None)
    declared = _declared(ui)
    if not declared:
        return {}
    approved = _approved(name, declared)
    parsed = UIConfig.from_dict(ui) if isinstance(ui, dict) else ui
    publishable = isinstance(parsed, UIConfig) and not parsed.frame_source_errors()
    staged = [origin for origin in declared if origin not in approved] if publishable else []
    return {"frameSourcesGrant": {"approved": approved, "staged": staged}}


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
            # Only origins the owner approved: an update or a live edit of app.json
            # that adds one waits on the app's page.
            approved = _approved(name, list(manifest.ui.frameSources))
        except Exception:  # noqa: BLE001 - one damaged app must not drop the others
            logger.warning("App %s: frameSources scan failed", name, exc_info=True)
            continue
        origins.update(approved)
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


__all__ = [
    "FRAME_SOURCE_GRANT_FAMILIES",
    "GRANT_FAMILY",
    "frame_source_grant_entries",
    "frame_src_extra",
    "frame_sources_grant_row",
    "refresh_frame_sources",
]
