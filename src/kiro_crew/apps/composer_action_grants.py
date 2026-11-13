"""What a composer action is allowed to do, as a normalized grant entry.

A composer action sees nothing of the composer: the host hands its frame no draft,
no selection and no session (``ComposerActionConfig``). Its whole authority is the
one effect its button has, offering text the host inserts as literal text. So the
grant is the button itself, ``<id>:insert``, stored per app in the approved-grants
file beside ``permissions.api`` and the link-preview families, and held back on an
update that adds one until the owner approves it (``manager_records``). A title or
icon change keeps the entry, so it keeps the approval; a new id is a new button and
waits. An entry the new manifest drops simply stops being declared.
"""

from __future__ import annotations

from typing import Any

#: The grant family this module derives.
COMPOSER_ACTION_FAMILY = "composerActions"
COMPOSER_ACTION_GRANT_FAMILIES = (COMPOSER_ACTION_FAMILY,)
#: The one effect a composer action has. A later effect is a new suffix, so an
#: update that adds one is a widening the owner approves.
INSERT_EFFECT = "insert"


def action_grant(action_id: str) -> str:
    """The grant entry of one composer action. Mirrored in ``composerActionRegistry.ts``."""
    return f"{action_id}:{INSERT_EFFECT}"


def _actions(contributes: Any) -> list[dict[str, Any]]:
    """The ``composerActions`` entries as plain dicts, from a manifest object or raw dict."""
    if contributes is None:
        return []
    raw = (
        contributes.get("composerActions")
        if isinstance(contributes, dict)
        else getattr(contributes, "composerActions", None)
    )
    out: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            out.append(entry)
        elif callable(to_dict := getattr(entry, "to_dict", None)):
            out.append(to_dict())
    return out


def composer_action_grant_entries(contributes: Any) -> dict[str, list[str]]:
    """The grant family a manifest's ``contributes`` declares."""
    entries: list[str] = []
    for action in _actions(contributes):
        action_id = action.get("id")
        if isinstance(action_id, str) and action_id:
            entry = action_grant(action_id)
            if entry not in entries:
                entries.append(entry)
    return {COMPOSER_ACTION_FAMILY: entries}


def approved_composer_actions(name: str, manifest: Any) -> list[dict[str, Any]]:
    """The declared actions whose grant the owner approved, in manifest order."""
    contributes = (
        manifest.get("contributes")
        if isinstance(manifest, dict)
        else getattr(manifest, "contributes", None)
    )
    actions = _actions(contributes)
    if not actions:
        return []
    declared = composer_action_grant_entries(contributes)[COMPOSER_ACTION_FAMILY]
    # Imported here: manager_records imports this module for its family.
    from kiro_crew.apps.manager import staged_app_grants

    granted = set(staged_app_grants(name, COMPOSER_ACTION_FAMILY, lambda: declared))
    return [a for a in actions if action_grant(str(a.get("id", ""))) in granted]


def composer_action_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``composerActionGrant`` key of an app row: approved actions, and ids that wait.

    ``actions`` are what the dashboard may render (id, title, icon,
    popover); ``staged`` are the ids an update added that wait for the owner.
    Absent for an app that declares no composer action.
    """
    contributes = (
        manifest.get("contributes")
        if isinstance(manifest, dict)
        else getattr(manifest, "contributes", None)
    )
    declared = _actions(contributes)
    if not declared:
        return {}
    approved = approved_composer_actions(name, manifest)
    approved_ids = {a.get("id") for a in approved}
    keys = ("id", "title", "icon", "popover")
    return {
        "composerActionGrant": {
            "actions": [{k: a[k] for k in keys if k in a} for a in approved],
            "staged": [
                str(a.get("id"))
                for a in declared
                if a.get("id") and a.get("id") not in approved_ids
            ],
        }
    }


__all__ = [
    "COMPOSER_ACTION_FAMILY",
    "COMPOSER_ACTION_GRANT_FAMILIES",
    "INSERT_EFFECT",
    "action_grant",
    "approved_composer_actions",
    "composer_action_grant_entries",
    "composer_action_grant_row",
]
