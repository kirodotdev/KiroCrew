"""What a selection action is allowed to see, as a normalized grant descriptor.

A ``contributes.selectionActions`` item is handed text the user selected. What it
may be handed is not its id: it is the sources it is offered on, the most text it
is given, and the host buttons it may offer a value for. Each item is therefore one
entry in the ``selectionActions`` grant family of the app's approved-grants file,
beside ``permissions.api`` / ``permissions.events``:

    ``<id>:<sources>:<minLength>-<maxLength>:<effects>``

with ``sources`` and ``effects`` sorted and comma-joined. Any change to an item is
a new entry, so an update that adds a source, raises ``maxLength`` or adds an
effect is held back until the owner approves it (``manager_records``), and the
item keeps no grant until then. Dropping an item needs nothing: it simply stops
being declared.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.apps.manifest_contributes import SelectionActionConfig

#: The grant family this module derives.
SELECTION_ACTION_GRANT_FAMILIES = ("selectionActions",)


def selection_action_entry(item: dict[str, Any]) -> str:
    """The grant entry of one item, from its serialized form.

    Mirrored by ``selectionGrantEntry`` in
    ``website/src/selectionActions/selectionActionRegistry.ts``.
    """
    sources = ",".join(sorted({str(s) for s in item.get("sources") or []}))
    effects = ",".join(sorted({str(e) for e in item.get("effects") or []}))
    return (
        f"{item.get('id', '')}:{sources}:"
        f"{item.get('minLength', 1)}-{item.get('maxLength', 0)}:{effects}"
    )


def _items(contributes: Any) -> list[dict[str, Any]]:
    """The declared items as plain dicts, from a manifest object or a raw dict."""
    if contributes is None:
        return []
    raw = (
        contributes.get("selectionActions")
        if isinstance(contributes, dict)
        else getattr(contributes, "selectionActions", None)
    )
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            # A raw manifest dict: parse it the way install does, so the entry
            # reads the resolved defaults rather than the author's spelling.
            item = SelectionActionConfig.from_dict(item)
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            d = to_dict()
            if isinstance(d.get("id"), str) and d["id"]:
                out.append(d)
    return out


def selection_action_grant_entries(contributes: Any) -> dict[str, list[str]]:
    """The ``selectionActions`` family a manifest's ``contributes`` declares."""
    entries: list[str] = []
    for item in _items(contributes):
        entry = selection_action_entry(item)
        if entry not in entries:
            entries.append(entry)
    return {"selectionActions": entries}


def approved_selection_action(name: str, manifest: Any, item_id: str) -> dict[str, Any] | None:
    """The serialized item ``item_id`` when its current entry is approved, else None."""
    contributes = (
        manifest.get("contributes")
        if isinstance(manifest, dict)
        else getattr(manifest, "contributes", None)
    )
    approved = set(_approved_entries(name, contributes))
    for item in _items(contributes):
        if item["id"] == item_id and selection_action_entry(item) in approved:
            return item
    return None


def _approved_entries(name: str, contributes: Any) -> list[str]:
    declared = selection_action_grant_entries(contributes)["selectionActions"]
    if not declared:
        return []
    # Imported here: manager_records imports this module's family.
    from kiro_crew.apps.manager import staged_app_grants

    def reader() -> list[str]:
        return declared

    return staged_app_grants(name, "selectionActions", reader)


def selection_action_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``selectionActionGrant`` key of an app row: approved and waiting items.

    ``approved`` holds the ids the dashboard may offer; ``staged`` the entries an
    update added or changed, which wait for the owner. Absent for an app that
    declares no item.
    """
    contributes = (
        manifest.get("contributes")
        if isinstance(manifest, dict)
        else getattr(manifest, "contributes", None)
    )
    items = _items(contributes)
    if not items:
        return {}
    approved = set(_approved_entries(name, contributes))
    return {
        "selectionActionGrant": {
            "approved": [i["id"] for i in items if selection_action_entry(i) in approved],
            "staged": [
                selection_action_entry(i)
                for i in items
                if selection_action_entry(i) not in approved
            ],
        }
    }


__all__ = [
    "SELECTION_ACTION_GRANT_FAMILIES",
    "approved_selection_action",
    "selection_action_entry",
    "selection_action_grant_entries",
    "selection_action_grant_row",
]
