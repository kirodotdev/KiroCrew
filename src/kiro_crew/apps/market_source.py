"""The ``market:`` source marker an installed marketplace app carries.

A leaf module with no Kiro Crew imports, so the trust resolver in the registry
pipeline and the marketplace installer read one spelling without importing each
other.

A marketplace install records ``source = "market:<namespace>/<name>@<n>"``. The
trust coordinate is that marker without the version: ``market:<namespace>/<name>``.
A per-app trust grant bound to it covers every version of that one listing, and
nothing else; a local-folder grant (``apps_trusted_local``) never covers it,
because the coordinate is non-empty.
"""

from __future__ import annotations

import re

MARKET_SOURCE_PREFIX = "market:"

_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
#: A listing ref: ``<namespace>/<name>``, both lowercase kebab segments.
LISTING_REF_RE = re.compile(rf"^{_SEGMENT}/{_SEGMENT}$")
_MARKET_SOURCE_RE = re.compile(rf"^market:({_SEGMENT}/{_SEGMENT})@([1-9][0-9]{{0,8}})$")


def market_source(ref: str, version: int) -> str:
    """The ``source`` value a marketplace install records."""
    return f"{MARKET_SOURCE_PREFIX}{ref}@{version}"


def is_market_source(source: object) -> bool:
    """Whether *source* claims marketplace provenance (well-formed or not)."""
    return isinstance(source, str) and source.startswith(MARKET_SOURCE_PREFIX)


def market_trust_coordinate(source: object) -> str:
    """The trust coordinate for a well-formed marketplace source, else ``""``."""
    if not isinstance(source, str):
        return ""
    match = _MARKET_SOURCE_RE.fullmatch(source)
    return f"{MARKET_SOURCE_PREFIX}{match.group(1)}" if match else ""
