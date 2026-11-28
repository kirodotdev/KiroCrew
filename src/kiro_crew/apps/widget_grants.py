"""What an app widget may be handed, as a normalized grant entry.

An app widget's module gets the parsed JSON payload of one
``<mcwidget use="<app>:<id>">`` tag. The payload is agent output, so it can hold
conversation text or a tool result. The widget frame is offline
(``widget_frame``): it has no capability and loads no network image. But the
module can still navigate its own frame to another host and carry the payload in
the address, and no frame policy a browser enforces stops that. So a widget draws
only with the owner's grant, as every other point handed conversation-derived text
does.

The grant is the widget itself, ``<id>:render``, stored per app in the
approved-grants file beside ``permissions.api`` and the other point families. An
update that adds a widget is held back until the owner approves it
(``manager_records``). A title, description or entry change keeps the entry, so it
keeps the approval. A new id is a new widget and waits. An entry the new manifest
drops simply stops being declared. The family is explicit-only
(``point_grants``): a set stored before this host knew ``contributes.widgets``
approves none, because no owner saw those widgets in a prompt, so each one waits
on the app page.
"""

from __future__ import annotations

from typing import Any

#: The grant family this module derives, which is also the manifest key.
WIDGET_GRANT_FAMILY = "widgets"
WIDGET_GRANT_FAMILIES = (WIDGET_GRANT_FAMILY,)
#: The one effect a widget has. A later effect is a new suffix, so an update that
#: adds one is a widening the owner approves.
RENDER_EFFECT = "render"


def widget_grant(widget_id: str) -> str:
    """The grant entry of one widget. Mirrored by ``widgetGrant`` in ``appWidgetRegistry.ts``."""
    return f"{widget_id}:{RENDER_EFFECT}"


def _contributes(manifest: Any) -> Any:
    if isinstance(manifest, dict):
        return manifest.get("contributes")
    return getattr(manifest, "contributes", None)


def _widget_ids(contributes: Any) -> list[str]:
    """The declared widget ids, once each, from a manifest object or a raw dict."""
    if contributes is None:
        return []
    if isinstance(contributes, dict):
        raw = contributes.get("widgets")
        ids = [w.get("id") for w in raw if isinstance(w, dict)] if isinstance(raw, list) else []
    else:
        # A parsed ``Contributes`` holds a ``WidgetsConfig``, whose list is ``widgets``.
        items = getattr(getattr(contributes, "widgets", None), "widgets", None)
        ids = [getattr(w, "id", None) for w in items] if isinstance(items, list) else []
    out: list[str] = []
    for widget_id in ids:
        if isinstance(widget_id, str) and widget_id and widget_id not in out:
            out.append(widget_id)
    return out


def widget_grant_entries(contributes: Any) -> dict[str, list[str]]:
    """The grant family a manifest's ``contributes`` declares."""
    return {WIDGET_GRANT_FAMILY: [widget_grant(i) for i in _widget_ids(contributes)]}


def approved_widget_ids(name: str, manifest: Any) -> list[str]:
    """The declared widget ids whose grant the owner approved, in manifest order."""
    ids = _widget_ids(_contributes(manifest))
    if not ids:
        return []
    declared = [widget_grant(i) for i in ids]
    # Imported here: manager_records imports the point registry, which names this family.
    from kiro_crew.apps.manager import staged_app_grants

    granted = set(staged_app_grants(name, WIDGET_GRANT_FAMILY, lambda: declared))
    return [i for i in ids if widget_grant(i) in granted]


def widget_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``widgetGrant`` key of an app row: the approved ids, and the ids that wait.

    ``approved`` are the widgets the dashboard may draw. ``staged`` are the ids an
    update added, which wait for the owner. Absent for an app that declares no widget.
    """
    ids = _widget_ids(_contributes(manifest))
    if not ids:
        return {}
    approved = approved_widget_ids(name, manifest)
    return {
        "widgetGrant": {
            "approved": approved,
            "staged": [i for i in ids if i not in approved],
        }
    }


__all__ = [
    "RENDER_EFFECT",
    "WIDGET_GRANT_FAMILIES",
    "WIDGET_GRANT_FAMILY",
    "approved_widget_ids",
    "widget_grant",
    "widget_grant_entries",
    "widget_grant_row",
]
