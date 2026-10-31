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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: ``contributes`` -> ``{family: entries}`` for one point's families.
EntriesFn = Callable[[Any], dict[str, list[str]]]

_REGISTRY: list[tuple[tuple[str, ...], EntriesFn]] = []


def register_point_grants(families: tuple[str, ...], entries: EntriesFn) -> None:
    """Register one point's grant families and how a manifest declares them."""
    taken = set(point_grant_families())
    clash = [f for f in families if f in taken or f in ("api", "events")]
    if clash:
        raise ValueError(f"grant families already registered: {clash}")
    _REGISTRY.append((tuple(families), entries))


def point_grant_families() -> tuple[str, ...]:
    """Every registered point family, in registration order."""
    return tuple(f for families, _ in _REGISTRY for f in families)


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
    else:
        perms_obj = getattr(manifest, "permissions", None)
        contributes = getattr(manifest, "contributes", None)
        api = getattr(perms_obj, "api", None)
        events = getattr(perms_obj, "events", None)
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
    return out


# Registered points. A new point that stores grants adds its line here.
from kiro_crew.apps.link_preview_grants import (  # noqa: E402
    LINK_PREVIEW_GRANT_FAMILIES,
    link_preview_grant_entries,
)

register_point_grants(LINK_PREVIEW_GRANT_FAMILIES, link_preview_grant_entries)


__all__ = [
    "EntriesFn",
    "grant_source",
    "point_grant_families",
    "register_point_grants",
]
