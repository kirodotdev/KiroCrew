"""What a link-preview provider is allowed to see, as a normalized grant descriptor.

A provider's authority is not its id: it is the hosts whose links it is shown and
the text patterns whose matches it is shown. An update that keeps the id but adds
a host or a pattern widens that authority, so both are stored per app in the
approved-grants file beside ``permissions.api`` / ``permissions.events``, and held
back on a widening update until the owner approves them (``manager_records``).
Narrowing needs nothing: an entry the new manifest drops simply stops being
declared.

Three grant families:

``linkPreviewHosts``
    Each host after normalization (:func:`normalize_host`): lowercase, no
    trailing dot, IDNA (punycode) form. A leading dot keeps its meaning, "this
    domain and every subdomain".
``linkPreviewTextPatterns``
    ``<id>:<flags>:<source>`` per pattern, flags sorted and without the implied
    ``g``. A regex cannot be judged narrower than another, so any change is a new
    entry.
``linkPreviewFullUrl``
    ``["fullUrl"]`` when the provider asks for whole URLs (query and fragment,
    where a signed link carries its credential), else empty. Turning it on is a
    widening like any other.

Hosts follow the one rule every point shares (``apps/app_hosts.py``): normalized,
and never a public suffix or a bare TLD. The families are registered with the other
points' in ``apps/point_grants.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Re-exported: the host rule lives in app_hosts, shared by every point that names hosts.
from kiro_crew.apps.app_hosts import (  # noqa: F401
    _CC_SECOND_LEVELS,
    PUBLIC_SUFFIXES,
    host_refusal,
    is_public_suffix,
    normalize_host,
)

#: Grant families this module derives, in the order they are stored.
LINK_PREVIEW_GRANT_FAMILIES = ("linkPreviewHosts", "linkPreviewTextPatterns", "linkPreviewFullUrl")
#: The one entry of ``linkPreviewFullUrl``.
FULL_URL_ENTRY = "fullUrl"


def pattern_grant(tp: dict[str, Any], index: int) -> str:
    """The grant entry of one text pattern: ``<id>:<flags>:<source>``.

    Mirrored by ``patternGrant`` in ``website/src/linkPreview/linkPreviewRegistry.ts``.
    """
    source = str(tp.get("pattern", ""))
    flags = "".join(sorted(set(str(tp.get("flags", "")).replace("g", ""))))
    pid = str(tp.get("id") or index)
    return f"{pid}:{flags}:{source}"


def _first_provider(contributes: Any) -> dict[str, Any] | None:
    """The first linkPreviews entry as a plain dict, from a manifest object or raw dict."""
    if contributes is None:
        return None
    previews = (
        contributes.get("linkPreviews")
        if isinstance(contributes, dict)
        else getattr(contributes, "linkPreviews", None)
    )
    if not isinstance(previews, list) or not previews:
        return None
    first = previews[0]
    if isinstance(first, dict):
        return first
    to_dict = getattr(first, "to_dict", None)
    return to_dict() if callable(to_dict) else None


def link_preview_grant_entries(contributes: Any) -> dict[str, list[str]]:
    """The two grant families a manifest's ``contributes`` declares."""
    provider = _first_provider(contributes)
    hosts: list[str] = []
    patterns: list[str] = []
    full_url: list[str] = []
    if provider is not None:
        if provider.get("fullUrl") is True:
            full_url.append(FULL_URL_ENTRY)
        raw_hosts = provider.get("hosts")
        for h in raw_hosts if isinstance(raw_hosts, list) else []:
            n = normalize_host(h) if isinstance(h, str) else None
            if n and n not in hosts:
                hosts.append(n)
        raw_patterns = provider.get("textPatterns")
        for i, tp in enumerate(raw_patterns if isinstance(raw_patterns, list) else []):
            if isinstance(tp, dict) and isinstance(tp.get("pattern"), str):
                entry = pattern_grant(tp, i)
                if entry not in patterns:
                    patterns.append(entry)
    return {
        "linkPreviewHosts": hosts,
        "linkPreviewTextPatterns": patterns,
        "linkPreviewFullUrl": full_url,
    }


def link_preview_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``linkPreviewGrant`` key of an app row: what the owner approved, and what waits.

    ``hosts`` / ``textPatterns`` are the approved, normalized entries the dashboard
    may use; ``stagedHosts`` / ``stagedTextPatterns`` are declared entries an
    update added that wait for the owner. Absent for an app with no provider.
    """
    contributes = (
        manifest.get("contributes")
        if isinstance(manifest, dict)
        else getattr(manifest, "contributes", None)
    )
    declared = link_preview_grant_entries(contributes)
    if not declared["linkPreviewHosts"] and not declared["linkPreviewTextPatterns"]:
        return {}
    # Imported here: manager_records imports this module for its families.
    from kiro_crew.apps.manager import staged_app_grants

    def reader(family: str) -> Callable[[], list[str]]:
        return lambda: declared[family]

    granted: dict[str, list[str]] = {}
    for family in LINK_PREVIEW_GRANT_FAMILIES:
        granted[family] = staged_app_grants(name, family, reader(family))
    return {
        "linkPreviewGrant": {
            "hosts": granted["linkPreviewHosts"],
            "textPatterns": granted["linkPreviewTextPatterns"],
            "stagedHosts": [
                h for h in declared["linkPreviewHosts"] if h not in granted["linkPreviewHosts"]
            ],
            "stagedTextPatterns": [
                p
                for p in declared["linkPreviewTextPatterns"]
                if p not in granted["linkPreviewTextPatterns"]
            ],
            # The dashboard sends whole URLs only when this was approved.
            "fullUrl": FULL_URL_ENTRY in granted["linkPreviewFullUrl"],
            "stagedFullUrl": bool(declared["linkPreviewFullUrl"])
            and FULL_URL_ENTRY not in granted["linkPreviewFullUrl"],
        }
    }


__all__ = [
    "link_preview_grant_row",
    "FULL_URL_ENTRY",
    "LINK_PREVIEW_GRANT_FAMILIES",
    "PUBLIC_SUFFIXES",
    "host_refusal",
    "is_public_suffix",
    "link_preview_grant_entries",
    "normalize_host",
    "pattern_grant",
]
