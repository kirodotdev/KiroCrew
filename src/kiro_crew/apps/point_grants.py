"""The grant families every contribution point contributes, and the manifest's full grant source.

What an app may do is not only ``permissions.api`` / ``permissions.events``: each
contribution point that discloses something to an app (a hovered link, a clicked
message) stores what the owner approved as its own entry families in the app's
``approved-grants.json``, held back on a widening update exactly like a new API
path (``manager_records``). Each point registers here:

``register_point_grants(families, entries)``
    ``families`` are the family names the point stores, in order; ``entries`` maps
    a manifest's ``contributes`` (a parsed ``Contributes`` or a raw dict) to
    ``{family: [entry, ...]}`` for those families. An entry is a string; a
    widening must produce an entry the older manifest did not, and a narrowing
    must only drop entries.

:func:`grant_source` is the one reader every install, update and approval path
uses: ``api``, ``events`` and every registered family a manifest declares.
:func:`point_grant_families` lists the registered families, in registration order.
The points shipped in this package register at the bottom of this module, so the
registry is complete once it is imported.

``register_permission_grants(families, entries, *, explicit_only=False)``
    The same, for a grant a manifest declares in ``permissions`` rather than in
    ``contributes`` (``permissions.workerAutoApprove``,
    ``permissions.reachableByUserSessions``): ``entries`` maps the
    manifest's ``permissions`` (parsed or a raw dict). Its families are staged and
    approved exactly like a point's, and :func:`point_grant_families` lists them too.
    ``explicit_only`` families are approved only by entries a stored set names: a
    stored set that lacks one approves nothing in it, where it would otherwise
    approve what the manifest declares (:func:`explicit_only_grant_families`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: ``contributes`` -> ``{family: entries}`` for one point's families.
EntriesFn = Callable[[Any], dict[str, list[str]]]

_REGISTRY: list[tuple[tuple[str, ...], EntriesFn]] = []
#: ``permissions`` -> ``{family: entries}`` for one permission's families.
_PERMISSION_REGISTRY: list[tuple[tuple[str, ...], EntriesFn]] = []
#: Families a stored set approves only by naming their entries.
_EXPLICIT_ONLY: list[str] = []


def _refuse_clash(families: tuple[str, ...]) -> None:
    taken = set(point_grant_families())
    clash = [f for f in families if f in taken or f in ("api", "events")]
    if clash:
        raise ValueError(f"grant families already registered: {clash}")


def register_point_grants(families: tuple[str, ...], entries: EntriesFn) -> None:
    """Register one point's grant families and how a manifest declares them."""
    _refuse_clash(families)
    _REGISTRY.append((tuple(families), entries))


def register_permission_grants(
    families: tuple[str, ...], entries: EntriesFn, *, explicit_only: bool = False
) -> None:
    """Register one permission's grant families, read from the manifest's ``permissions``.

    ``explicit_only`` suits a permission that a manifest could declare before this
    host knew the key: the owner was never shown it, so a stored set written then
    must not approve it.
    """
    _refuse_clash(families)
    _PERMISSION_REGISTRY.append((tuple(families), entries))
    if explicit_only:
        _EXPLICIT_ONLY.extend(families)


def point_grant_families() -> tuple[str, ...]:
    """Every registered point family, then every permission family, in registration order."""
    return tuple(f for families, _ in (*_REGISTRY, *_PERMISSION_REGISTRY) for f in families)


def explicit_only_grant_families() -> tuple[str, ...]:
    """The registered families a stored set that lacks them approves nothing in."""
    return tuple(_EXPLICIT_ONLY)


def grant_source(manifest: Any) -> dict[str, list[str]] | None:
    """Every staged grant family a manifest declares, as one mapping.

    Accepts a parsed ``AppManifest`` or a raw manifest dict, and returns ``None``
    for ``None``, so a caller that has no manifest keeps passing ``None``.
    """
    if manifest is None:
        return None
    if isinstance(manifest, dict):
        perms = manifest.get("permissions")
        contributes = manifest.get("contributes")
        perms = perms if isinstance(perms, dict) else {}
        api = perms.get("api")
        events = perms.get("events")
        permissions_view: Any = perms
    else:
        perms_obj = getattr(manifest, "permissions", None)
        contributes = getattr(manifest, "contributes", None)
        api = getattr(perms_obj, "api", None)
        events = getattr(perms_obj, "events", None)
        permissions_view = perms_obj
    out: dict[str, list[str]] = {
        "api": [e for e in api if isinstance(e, str) and e] if isinstance(api, list) else [],
        "events": (
            [e for e in events if isinstance(e, str) and e] if isinstance(events, list) else []
        ),
    }
    for families, entries in _REGISTRY:
        declared = entries(contributes)
        for family in families:
            out[family] = list(declared.get(family, []))
    for families, entries in _PERMISSION_REGISTRY:
        declared = entries(permissions_view)
        for family in families:
            out[family] = list(declared.get(family, []))
    return out


# Registered points. A new point that stores grants adds its line here.
from kiro_crew.apps.link_preview_grants import (  # noqa: E402
    LINK_PREVIEW_GRANT_FAMILIES,
    link_preview_grant_entries,
)
from kiro_crew.apps.message_action_grants import (  # noqa: E402
    MESSAGE_ACTION_GRANT_FAMILIES,
    message_action_grant_entries,
)

register_point_grants(LINK_PREVIEW_GRANT_FAMILIES, link_preview_grant_entries)
register_point_grants(MESSAGE_ACTION_GRANT_FAMILIES, message_action_grant_entries)

from kiro_crew.apps.link_open_grants import (  # noqa: E402
    LINK_OPEN_GRANT_FAMILIES,
    link_open_grant_entries,
)

register_point_grants(LINK_OPEN_GRANT_FAMILIES, link_open_grant_entries)
from kiro_crew.apps.composer_action_grants import (  # noqa: E402
    COMPOSER_ACTION_GRANT_FAMILIES,
    composer_action_grant_entries,
)

register_point_grants(COMPOSER_ACTION_GRANT_FAMILIES, composer_action_grant_entries)

from kiro_crew.apps.selection_action_grants import (  # noqa: E402
    SELECTION_ACTION_GRANT_FAMILIES,
    selection_action_grant_entries,
)

register_point_grants(SELECTION_ACTION_GRANT_FAMILIES, selection_action_grant_entries)

from kiro_crew.apps.worker_auto_approve import (  # noqa: E402
    WORKER_AUTO_APPROVE_GRANT_FAMILIES,
    worker_auto_approve_grant_entries,
)

register_permission_grants(WORKER_AUTO_APPROVE_GRANT_FAMILIES, worker_auto_approve_grant_entries)

from kiro_crew.apps.user_session_reach import (  # noqa: E402
    USER_SESSION_REACH_GRANT_FAMILIES,
    user_session_reach_grant_entries,
)

register_permission_grants(
    USER_SESSION_REACH_GRANT_FAMILIES, user_session_reach_grant_entries, explicit_only=True
)


__all__ = [
    "EntriesFn",
    "explicit_only_grant_families",
    "grant_source",
    "point_grant_families",
    "register_permission_grants",
    "register_point_grants",
]
