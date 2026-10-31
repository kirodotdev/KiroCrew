"""Host names an app point claims: one normalization and one refusal rule for every point.

A point that names hosts (``linkPreviews`` today; any point that claims links or
fetches a URL it was handed) stores, matches and grants each host in ONE form, so
what the owner approved is what is matched:

- lowercase, one trailing dot dropped, each label in IDNA (punycode) form;
- a leading dot kept, meaning "this domain and every subdomain".

A host is refused when it is not a hostname, or is a public suffix or bare TLD
(``co.uk``, ``github.io``, ``com``): such a claim would reach every site under it.
The check is a curated list plus a rule for the common ccTLD second levels, not the
full Public Suffix List, which this package does not ship;
``website/src/linkPreview/linkPreviewRegistry.ts`` mirrors both.
"""

from __future__ import annotations

import re

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


__all__ = ["PUBLIC_SUFFIXES", "host_refusal", "is_public_suffix", "normalize_host"]
