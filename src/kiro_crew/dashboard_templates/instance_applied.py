"""What a crewmate's record can say of the package pages that were ever in force.

``dashboard_add_field`` asks this to tell a true draft (a stored page that was never put
in force) from a page the crewmate left (one a kept version pins). The record itself is
:mod:`kiro_crew.dashboard_templates.instance`'s; this module only reads it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kiro_crew.dashboard_templates import instance


def page_digest(content: str) -> str:
    """The digest a version pins a package page by: of its canonical stored bytes."""
    return instance._page_digest(content)


@dataclass(frozen=True)
class AppliedPages:
    """What the record and its kept versions say.

    ``pins``: the digest of every page the record and its kept versions pin, each with
    the time of the newest version that pins it (a page written after that time has
    the same bytes and is another page: it was not in force). ``keyed``: the record
    itself says whether it pins a page (it carries the ``package`` key; one written
    before pins does not, see ``instance.keeps_older_package_rule``).
    ``tellable_since_ms``: a page written at or after this time and pinned nowhere was
    never in force; of one written before it the record cannot say. It is 0 when every
    version ever written is still kept and each says what it pinned. Else it is the
    newest of: the oldest kept version's time, when older ones were pruned
    (``instance.MAX_RETAINED_VERSIONS``); the time of the newest kept version that
    carries no ``package`` key.
    """

    pins: Mapping[str, int]
    keyed: bool
    tellable_since_ms: int


def applied_pages(slug: str) -> AppliedPages:
    """Read :class:`AppliedPages` for *slug*. COMPLETE OR RAISES, as
    ``instance._referenced_digests``: a version that cannot be listed or read may pin a page."""
    raw = instance._read_record(slug)
    try:
        names = instance._list_versions(slug)
    except FileNotFoundError:
        names = []
    numbers = sorted(
        int(n[: -len(".json")])
        for n in names
        if n.endswith(".json") and n[: -len(".json")].isdigit()
    )
    kept = [(n, instance._read_version(slug, n)) for n in numbers]
    pins: dict[str, int] = {}
    for r in [*(v for _n, v in kept), raw]:
        if not isinstance(r, dict):
            continue
        digest = instance._package_field(r)[2]
        if digest:
            pins[digest] = max(pins.get(digest, 0), instance._updated_ms(r))
    since = [instance._updated_ms(v) for _n, v in kept if "package" not in v]
    if kept and kept[0][0] > 1:
        since.append(instance._updated_ms(kept[0][1]))
    return AppliedPages(
        pins=pins,
        keyed=isinstance(raw, Mapping) and "package" in raw,
        tellable_since_ms=max(since, default=0),
    )
