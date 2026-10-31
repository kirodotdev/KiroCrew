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

Hosts must not claim a public suffix (``co.uk``, ``github.io``): such a claim would
reach every site under it. The check is a curated list plus a rule for the common
ccTLD second levels, not the full Public Suffix List, which this package does not
ship; ``website/src/linkPreview/linkPreviewRegistry.ts`` mirrors both.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: Grant families this module derives, in the order they are stored.
LINK_PREVIEW_GRANT_FAMILIES = ("linkPreviewHosts", "linkPreviewTextPatterns", "linkPreviewFullUrl")
#: The one entry of ``linkPreviewFullUrl``.
FULL_URL_ENTRY = "fullUrl"

#: Characters transitional and non-transitional IDNA map differently.
_IDNA_DEVIATIONS = frozenset("\u00df\u03c2\u200c\u200d")

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

#: Second-level labels a country-code TLD commonly sells under (``co.uk``,
#: ``com.au``, ``ac.jp``). ``<label>.<two letters>`` is treated as a public suffix.
_CC_SECOND_LEVELS = frozenset(
    {
        "ac",
        "co",
        "com",
        "edu",
        "gob",
        "go",
        "gov",
        "ltd",
        "mil",
        "ne",
        "net",
        "nic",
        "or",
        "org",
        "plc",
        "sch",
    }
)

#: Suffixes under which anyone can publish a site. Claiming one would match every
#: tenant. Mirrored in ``linkPreviewRegistry.ts`` (``PUBLIC_SUFFIXES``).
PUBLIC_SUFFIXES = frozenset(
    {
        "appspot.com",
        "azurewebsites.net",
        "blogspot.com",
        "cloudfront.net",
        "firebaseapp.com",
        "github.io",
        "gitlab.io",
        "herokuapp.com",
        "netlify.app",
        "ngrok.io",
        "pages.dev",
        "vercel.app",
        "web.app",
        "workers.dev",
    }
)


def normalize_host(raw: str) -> str | None:
    """The canonical form of a manifest host, or None when it is not a hostname.

    Lowercase, one trailing dot dropped, each label in IDNA (punycode) form. A
    leading dot is kept. Validity of the result is :func:`host_refusal`'s job.
    """
    s = str(raw).strip().lower()
    lead = s.startswith(".")
    if lead:
        s = s[1:]
    if s.endswith("."):
        s = s[:-1]
    if not s:
        return None
    # Python's codec is transitional IDNA 2003: it maps the deviation characters
    # (ß to ss, ς to σ, and drops ZWJ/ZWNJ) where a browser keeps them (UTS 46
    # non-transitional), so the two would name different domains. Such a host is
    # refused rather than normalized to the wrong one.
    if _IDNA_DEVIATIONS & set(s):
        return None
    try:
        ascii_host = s.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return ("." if lead else "") + ascii_host


def is_public_suffix(host: str) -> bool:
    """Whether a normalized host (no leading dot) is a public suffix or a bare TLD."""
    labels = host.split(".")
    if len(labels) < 2:
        return True
    if host in PUBLIC_SUFFIXES:
        return True
    return len(labels) == 2 and len(labels[1]) == 2 and labels[0] in _CC_SECOND_LEVELS


def host_refusal(host: str) -> str | None:
    """Why a normalized host cannot be declared, or None when it can."""
    bare = host[1:] if host.startswith(".") else host
    labels = bare.split(".")
    if len(bare) > 253 or any(not _LABEL_RE.fullmatch(lbl) or len(lbl) > 63 for lbl in labels):
        return "not a hostname"
    if is_public_suffix(bare):
        return "a public suffix or a bare top-level domain"
    return None


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
    out.update(link_preview_grant_entries(contributes))
    return out


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
    "grant_source",
    "host_refusal",
    "is_public_suffix",
    "link_preview_grant_entries",
    "normalize_host",
    "pattern_grant",
]
