"""What a message action is allowed to see, as grant entries the owner approves.

A message action's authority is which messages it can be offered on and in what
form it gets their text, not its id. Each is one entry of the ``messageActions``
family, stored beside ``permissions.api`` / ``permissions.events`` in the
approved-grants file and held back on a widening update until the owner approves
it (``manager_records``):

``<id>:role:assistant`` / ``<id>:role:user``
    The action is offered on assistant replies / on the user's own messages.
``<id>:format:markdown``
    The action gets the markdown source instead of the rendered plain text.

Plain text needs no entry of its own: it is the narrowest form. ``maxChars`` is
not a grant: it is capped at 32,000 by the manifest, and a longer cap reveals
more of a message the user already chose to hand over, not another message.

An action with no approved role is not offered. An action whose ``markdown`` is
waiting keeps working with plain text, its approved form.
"""

from __future__ import annotations

from typing import Any

#: Grant families this module derives.
MESSAGE_ACTION_GRANT_FAMILIES = ("messageActions",)
_FAMILY = MESSAGE_ACTION_GRANT_FAMILIES[0]


def _actions(contributes: Any) -> list[dict[str, Any]]:
    """The messageActions entries as plain dicts, from a manifest object or raw dict."""
    if contributes is None:
        return []
    raw = (
        contributes.get("messageActions")
        if isinstance(contributes, dict)
        else getattr(contributes, "messageActions", None)
    )
    out: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            out.append(entry)
            continue
        to_dict = getattr(entry, "to_dict", None)
        if callable(to_dict):
            out.append(to_dict())
    return out


def _entries_of(action: dict[str, Any]) -> list[str]:
    aid = action.get("id")
    if not isinstance(aid, str) or not aid:
        return []
    roles = action.get("roles", ["assistant"])
    entries = [
        f"{aid}:role:{r}"
        for r in (roles if isinstance(roles, list) else [])
        if r in ("assistant", "user")
    ]
    if action.get("format") == "markdown":
        entries.append(f"{aid}:format:markdown")
    return entries


def message_action_grant_entries(contributes: Any) -> dict[str, list[str]]:
    """The ``messageActions`` grant family a manifest's ``contributes`` declares."""
    entries: list[str] = []
    for action in _actions(contributes):
        for e in _entries_of(action):
            if e not in entries:
                entries.append(e)
    return {_FAMILY: entries}


def message_action_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``messageActionGrant`` key of an app row: each action at its approved reach.

    ``actions`` lists every declared action that has at least one approved role,
    with ``roles`` and ``format`` cut down to what the owner approved; ``staged``
    lists the declared entries an update added that wait for the owner. Absent for
    an app with no message action.
    """
    contributes = (
        manifest.get("contributes")
        if isinstance(manifest, dict)
        else getattr(manifest, "contributes", None)
    )
    actions = _actions(contributes)
    declared = message_action_grant_entries(contributes)[_FAMILY]
    if not declared:
        return {}
    # Imported here: manager_records imports this module for its family.
    from kiro_crew.apps.manager import staged_app_grants

    def reader() -> list[str]:
        return declared

    # Declared AND approved, read fresh: the same answer every enforcement point gets.
    approved = set(staged_app_grants(name, _FAMILY, reader))
    rows: list[dict[str, Any]] = []
    for action in actions:
        aid = str(action.get("id") or "")
        roles = [r for r in ("assistant", "user") if f"{aid}:role:{r}" in approved]
        if not roles:
            continue
        raw_popover = action.get("popover")
        popover: dict[str, Any] = raw_popover if isinstance(raw_popover, dict) else {}
        rows.append(
            {
                "id": aid,
                "title": str(action.get("title") or ""),
                "roles": roles,
                "format": "markdown" if f"{aid}:format:markdown" in approved else "plain",
                "maxChars": action.get("maxChars", 32000),
                "popover": {
                    "width": popover.get("width", 360),
                    "maxHeight": popover.get("maxHeight", 480),
                },
            }
        )
    return {
        "messageActionGrant": {
            "actions": rows,
            "staged": [e for e in declared if e not in approved],
        }
    }


def approved_message_action(name: str, manifest: Any, action_id: str) -> dict[str, Any] | None:
    """One action's row from :func:`message_action_grant_row`, or None when not offered."""
    grant = message_action_grant_row(name, manifest).get("messageActionGrant") or {}
    for row in grant.get("actions", []):
        if row.get("id") == action_id:
            return dict(row)
    return None


__all__ = [
    "MESSAGE_ACTION_GRANT_FAMILIES",
    "approved_message_action",
    "message_action_grant_entries",
    "message_action_grant_row",
]
