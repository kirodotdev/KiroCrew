"""What a link-open entry is allowed to see, as grant families.

A ``contributes.linkOpen`` entry's authority is the hosts whose clicked links it is
handed, and whether it is handed them whole. Both are grant families stored per
app beside ``permissions.api`` and held back on a widening update until the owner
approves them (registered in ``apps/point_grants.py``):

``linkOpenHosts``
    Each host after :func:`kiro_crew.apps.app_hosts.normalize_host`.
``linkOpenFullUrl``
    ``["fullUrl"]`` when the entry asks for whole URLs (query and fragment, where a
    signed link carries its credential), else empty.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew.apps.app_hosts import normalized_hosts

#: Grant families ``contributes.linkOpen`` derives, in the order they are stored.
LINK_OPEN_GRANT_FAMILIES = ("linkOpenHosts", "linkOpenFullUrl")
#: The one entry of ``linkOpenFullUrl``.
FULL_URL_ENTRY = "fullUrl"


def _first_entry(contributes: Any) -> dict[str, Any] | None:
    """The first linkOpen entry as a plain dict, from a manifest object or raw dict."""
    if contributes is None:
        return None
    entries = (
        contributes.get("linkOpen")
        if isinstance(contributes, dict)
        else getattr(contributes, "linkOpen", None)
    )
    if not isinstance(entries, list) or not entries:
        return None
    first = entries[0]
    if isinstance(first, dict):
        return first
    to_dict = getattr(first, "to_dict", None)
    return to_dict() if callable(to_dict) else None


def link_open_grant_entries(contributes: Any) -> dict[str, list[str]]:
    """The link-open grant families a manifest's ``contributes`` declares."""
    entry = _first_entry(contributes)
    hosts: list[str] = []
    full_url: list[str] = []
    if entry is not None:
        hosts = normalized_hosts(entry.get("hosts"))
        if entry.get("fullUrl") is True:
            full_url.append(FULL_URL_ENTRY)
    return {"linkOpenHosts": hosts, "linkOpenFullUrl": full_url}


def link_open_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``linkOpenGrant`` key of an app row: what the owner approved, and what waits.

    ``hosts`` are the approved, normalized hosts the dashboard may hand the app on a
    click; ``stagedHosts`` are declared hosts an update added that wait for the
    owner. Absent for an app with no ``linkOpen`` entry.
    """
    contributes = (
        manifest.get("contributes")
        if isinstance(manifest, dict)
        else getattr(manifest, "contributes", None)
    )
    declared = link_open_grant_entries(contributes)
    if not declared["linkOpenHosts"]:
        return {}
    # Imported here: manager_records imports the grant registry, which imports this.
    from kiro_crew.apps.manager import staged_app_grants

    def reader(family: str) -> Callable[[], list[str]]:
        return lambda: declared[family]

    granted = {
        family: staged_app_grants(name, family, reader(family))
        for family in LINK_OPEN_GRANT_FAMILIES
    }
    return {
        "linkOpenGrant": {
            "hosts": granted["linkOpenHosts"],
            "stagedHosts": [
                h for h in declared["linkOpenHosts"] if h not in granted["linkOpenHosts"]
            ],
            # The dashboard sends whole URLs only when this was approved.
            "fullUrl": FULL_URL_ENTRY in granted["linkOpenFullUrl"],
            "stagedFullUrl": bool(declared["linkOpenFullUrl"])
            and FULL_URL_ENTRY not in granted["linkOpenFullUrl"],
        }
    }


__all__ = [
    "FULL_URL_ENTRY",
    "LINK_OPEN_GRANT_FAMILIES",
    "link_open_grant_entries",
    "link_open_grant_row",
]
