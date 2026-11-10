"""Whether a newer version of an installed app is already published.

Answers from the caches the store keeps under the data home and NEVER fetches,
so an installed app can be told about a new release without that question
costing a network round trip on every read of its own record.

A cold cache answers ``None``, not "no update". The difference matters to the
surface asking: an app told "no update" when nothing has been consulted yet
would claim it is current on no evidence, and a user would read a quiet badge
as a checked one. ``None`` lets the API omit the field instead.

Every answer carries ``checkedAt`` because every answer may be stale. The
caches have their own TTLs, and this reader ignores them on purpose: a reader
that cannot refresh has a stale answer or no answer at all, and reporting WHEN
the shelf was last seen is more useful than withholding the version. The age is
the caller's to judge.

**Advisory only.** This decides what a surface SHOWS, never what an update is
permitted to do. External index caches and the catalog cache are both
agent-writable, so a planted version here can claim an update exists; that
costs a wrong badge and nothing more, because applying one re-resolves its
source authoritatively through ``_resolve_install_entry``, which never reads
these caches for coordinates. Same boundary ``official_catalog`` draws for its
own cache: annotating a row that exists anyway is safe, materialising a row is
not.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.apps import official_catalog
from kiro_crew.apps.registry_pipeline.caches import (
    _external_registry_cache_identity,
    _external_registry_cache_path,
    _manifest_cache_path,
    _read_external_registry_cache,
)
from kiro_crew.apps.registry_pipeline.catalog import _version_newer, is_registry_source
from kiro_crew.apps.registry_pipeline.git_targets import (
    _entry_git_url,
    _public_registry_name,
    _same_git_target,
)
from kiro_crew.apps.registry_pipeline.sources import (
    _effective_registries,
    _is_pinned_registry_move,
    _load_registry_file,
)

logger = logging.getLogger(__name__)


def _iso_utc(mtime: float | None) -> str | None:
    """A cache file's mtime as an ISO-8601 UTC instant."""
    if mtime is None:
        return None
    try:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        # A clock far outside the representable range is not worth failing a
        # listing over; the version is still the useful half of the answer.
        return None


def _cache_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


#: The version shapes this module will repeat to a surface: semver core, with an
#: optional pre-release and build part drawn from semver's own alphabet.
#: Deliberately strict, because a published version is text a registry author
#: controls and the bell note prints it in the gateway's voice. Spaces, colons
#: and punctuation outside ``[0-9A-Za-z.-]`` are what turn ``9.0.0-...`` into
#: a sentence, and none of them can appear here.
_PUBLISHABLE_VERSION = re.compile(
    r"\d{1,9}\.\d{1,9}\.\d{1,9}(?:-[0-9A-Za-z.-]{1,32})?(?:\+[0-9A-Za-z.-]{1,32})?"
)


def publishable_version(value: Any) -> str:
    """*value* if it is a version this module will repeat, else ``""``.

    ``""`` and not an error: a version a surface may not show is treated as no
    usable version at all, which every caller already handles as "say nothing".
    """
    if isinstance(value, str) and _PUBLISHABLE_VERSION.fullmatch(value):
        return value
    return ""


@dataclass(frozen=True)
class _Claim:
    """One source's row for an app name, in the order the sources are searched."""

    #: ``""`` for the catalog and the bundled seed, else the external registry's
    #: ``_registry`` tag, which is what an install records as ``sourceRegistry``.
    registry: str
    git_url: str
    published: tuple[str, float | None]
    seed: bool = False


def _claims() -> dict[str, list[_Claim]]:
    """Every source's row for every name the caches hold, in search order.

    The order is ``published_index``'s: the published catalog, then the bundled
    seed, then each configured registry in config order.
    """
    claims: dict[str, list[_Claim]] = {}

    def _add(name: Any, claim: _Claim) -> None:
        if isinstance(name, str) and name:
            claims.setdefault(name, []).append(claim)

    entries, catalog_mtime = official_catalog.cached_entries()
    for entry in entries:
        # The catalog document carries its clone URL inside ``source``, read by
        # the validator ``inventory`` uses. The URL only MATCHES a recorded
        # source here, never feeds an install, so the agent-writable cache is
        # trusted no further than it already is for the version beside it.
        coords = official_catalog._coordinates(entry.get("source"))
        _add(
            entry.get("name"),
            _Claim(
                "",
                coords["url"] if coords else "",
                (publishable_version(entry.get("version")), catalog_mtime),
            ),
        )

    # Claims only: a seed row describes what this wheel shipped, not what is
    # published, so its own version (when it has one) is no answer to "is a
    # newer release out?". It reserves the name and nothing more.
    for entry in _load_registry_file():
        if isinstance(entry, dict):
            _add(entry.get("name"), _Claim("", _entry_git_url(entry), ("", None), seed=True))

    for reg in _effective_registries():
        cache_name = _external_registry_cache_identity(reg)
        # ``ignore_ttl`` is the contract for a synchronous caller that cannot
        # refresh the cache itself, which is exactly this reader. The age is
        # reported rather than hidden; see the module docstring.
        cached = _read_external_registry_cache(cache_name, ignore_ttl=True)
        if not cached:
            continue
        mtime = _cache_mtime(_external_registry_cache_path(cache_name))
        for entry in cached:
            version, seen = entry.get("version"), mtime
            if not publishable_version(version):
                # A registry whose rows carry no version (the app's own app.json
                # owns it) is answered from that app.json as the store last fetched
                # it, the same file the store's own Update button reads.
                found = _manifest_version(entry)
                if found is not None:
                    version, seen = found
            # The registry is the index this row was read out of, not a tag in
            # the cache file, so the badge and ``_pinned_registry_entry`` agree.
            _add(
                entry.get("name"),
                _Claim(
                    _public_registry_name(reg),
                    _entry_git_url(entry),
                    (publishable_version(version), seen),
                ),
            )
    return claims


def _first_claim(claims: list[_Claim]) -> _Claim:
    """The row a bare-name lookup resolves, as ``_resolve_registry_row`` picks it.

    The first source to name the app, except that a seed row outranks a catalog
    row describing a DIFFERENT repository: the catalog may not rebind a bundled
    name, so the update installs the seed's row, which publishes no version.
    """
    first = claims[0]
    seed = next((c for c in claims if c.seed), None)
    if seed is not None and not first.seed and not _same_git_target(seed.git_url, first.git_url):
        return seed
    return first


def _recorded_source(record: dict[str, Any]) -> tuple[str, str] | None:
    """``(sourceUrl, sourceRegistry)`` an installed registry app was installed from.

    ``None`` for a record without provenance (installed before it was recorded,
    or not from a registry): its update resolves the bare name.
    """
    source = record.get("source")
    url = record.get("sourceUrl")
    if not (isinstance(source, str) and is_registry_source(source)):
        return None
    if not isinstance(url, str) or not url:
        return None
    registry = record.get("sourceRegistry")
    return url, registry if isinstance(registry, str) else ""


def _pinned_claim(claims: list[_Claim], url: str, registry: str) -> _Claim | None:
    """The row ``_pinned_registry_entry`` would update from.

    Same URL and same registry first; failing that, the same URL in another
    build-pinned registry when the recorded one is pinned too, the move
    ``_pinned_registry_entry`` follows.
    """
    for claim in claims:
        if not _same_git_target(claim.git_url, url):
            continue
        if claim.registry != registry and not _same_git_target(claim.registry, registry):
            continue
        return claim
    for claim in claims:
        if _same_git_target(claim.git_url, url) and _is_pinned_registry_move(
            registry, claim.registry
        ):
            return claim
    return None


def published_index(
    installed: list[dict[str, Any]] | None = None,
) -> dict[str, tuple[str, float | None]]:
    """Every published version the caches name: ``app -> (version, mtime)``.

    Built in one pass so a caller stamping a whole list of apps reads each cache
    file once rather than once per app.

    The answer for an app is the row its Update installs from. An installed
    registry app that recorded its source (``sourceUrl`` and ``sourceRegistry``)
    is answered from the row with that URL in that registry, the row
    ``_pinned_registry_entry`` selects; with no such row cached it gets no
    answer. A registry that publishes the same name ahead of it never answers
    for it, in either direction.

    Every other name follows ``_resolve_registry_row``: the published catalog,
    then the bundled seed, then each configured registry in config order. The
    FIRST source to name an app wins, for the reason resolution works that way:
    app trust is keyed by NAME, so a same-named row from an owner-configured
    registry must never answer for an app the catalog or the seed already
    names. The seed is read from the wheel, not a cache, and is what keeps that
    true when the catalog cache is cold.

    *installed* is the installed records to answer for; ``None`` reads them.

    A source that names an app without a usable version claims it with an empty
    version string rather than being skipped. That is what stops the search at
    the claimant instead of falling through to a source that would describe a
    different app's releases, and an empty version yields no answer downstream.
    """
    claims = _claims()
    index = {name: _first_claim(rows).published for name, rows in claims.items()}
    if installed is None:
        from kiro_crew.apps.manager import list_apps

        installed = list_apps()
    for record in installed:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        recorded = _recorded_source(record)
        if not isinstance(name, str) or not name or recorded is None:
            continue
        pinned = _pinned_claim(claims.get(name, []), *recorded)
        if pinned is None:
            index.pop(name, None)
        else:
            index[name] = pinned.published
    return index


def _manifest_version(entry: dict[str, Any]) -> tuple[Any, float | None] | None:
    """``(version, mtime)`` from the cached ``app.json`` of *entry*'s exact source, or None.

    Read whatever its age, like the index caches above: this reader cannot
    refresh, and ``checkedAt`` reports how old the answer is. The cache path is
    derived from the row's full source coordinates, so another branch or
    repository can never answer, and a manifest naming a different app is
    ignored, the same mismatch install refuses. Advisory like everything here: a
    planted file costs a wrong badge, never an install.
    """
    try:
        path = _manifest_cache_path(entry)
        if path.is_symlink():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(data, dict) or data.get("name") != entry.get("name"):
        return None
    return data.get("version"), _cache_mtime(path)


def _status_for(
    published: tuple[str, float | None] | None, installed_version: str
) -> dict[str, Any] | None:
    """One app's update state from its index entry, or ``None`` for no answer.

    ``checkedAt`` may be ``None`` while a version is known, when the cache file
    is readable but its mtime is not; the version is not withheld for it.
    ``available`` is ``False`` whenever the comparison cannot be made, matching
    ``_version_newer``'s conservative answer: an update nobody can confirm is
    not offered.
    """
    if published is None:
        return None
    latest, mtime = published
    if not latest:
        return None
    return {
        "available": _version_newer(latest, installed_version),
        "latestVersion": latest,
        "checkedAt": _iso_utc(mtime),
    }


def _stamp(row: dict[str, Any], published: tuple[str, float | None] | None) -> dict[str, Any]:
    """Put ``update`` on *row* when there is an answer, and leave it off when not.

    The key is OMITTED, not set false, when nothing is known. A reader cannot
    otherwise tell "nothing newer is published" from "nothing has been consulted
    yet", and a surface rendering the second as the first tells the user the app
    is current on no evidence at all.
    """
    update = _status_for(published, str(row.get("version") or ""))
    if update is not None:
        row["update"] = update
    return row


def attach_update(name: str, detail: dict[str, Any]) -> dict[str, Any]:
    """Put ``update`` on ONE installed app's row. Mutates and returns *detail*.

    Costs the caller no network call, which is what makes it safe on a row
    fetched every time a user opens an app: a version check reaching a registry
    from here would put a remote host on that path. Producing fresh answers
    stays the listing refresh's job; this reads what that left behind.

    For several rows use :func:`attach_updates`, which reads the caches once.
    """
    return _stamp(detail, published_index([{**detail, "name": name}]).get(name))


def attach_updates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put ``update`` on every row in *rows*, reading the caches once.

    Every host surface must answer the same way. An app's banner is written
    once and mounts both as a routed page and as a side-panel tab, and the two
    read their row from different endpoints -- so a field present on one and
    absent on the other would have the app claim nothing is known about itself
    on half its surfaces, silently, because absent MEANS unknown.

    Keyed by each row's own ``name``; a row without one is left untouched.
    """
    index = published_index(rows)
    for row in rows:
        name = row.get("name")
        if isinstance(name, str) and name:
            _stamp(row, index.get(name))
    return rows
