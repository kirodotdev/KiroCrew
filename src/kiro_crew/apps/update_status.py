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

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.apps import official_catalog
from kiro_crew.apps.registry_pipeline.caches import (
    _external_registry_cache_identity,
    _external_registry_cache_path,
    _read_external_registry_cache,
)
from kiro_crew.apps.registry_pipeline.catalog import _version_newer
from kiro_crew.apps.registry_pipeline.sources import _effective_registries, _load_registry_file

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


def published_index() -> dict[str, tuple[str, float | None]]:
    """Every published version the caches name: ``app -> (version, mtime)``.

    Built in one pass so a caller stamping a whole list of apps reads each cache
    file once rather than once per app.

    Precedence follows ``_resolve_registry_row``: the published catalog, then
    the bundled seed, then each configured registry in config order. The FIRST
    source to name an app wins, for the reason resolution works that way: app
    trust is keyed by NAME, so a same-named row from an owner-configured
    registry must never answer for an app the catalog or the seed already
    names. The seed is read from the wheel, not a cache, and is what keeps that
    true when the catalog cache is cold.

    A source that names an app without a usable version claims it with an empty
    version string rather than being skipped. That is what stops the search at
    the claimant instead of falling through to a source that would describe a
    different app's releases, and an empty version yields no answer downstream.
    """
    index: dict[str, tuple[str, float | None]] = {}

    def _claim(name: Any, version: Any, mtime: float | None) -> None:
        if not isinstance(name, str) or not name or name in index:
            return
        index[name] = (publishable_version(version), mtime)

    entries, catalog_mtime = official_catalog.cached_entries()
    for entry in entries:
        _claim(entry.get("name"), entry.get("version"), catalog_mtime)

    # Claims only: a seed row describes what this wheel shipped, not what is
    # published, so its own version (when it has one) is no answer to "is a
    # newer release out?". It reserves the name and nothing more.
    for entry in _load_registry_file():
        if isinstance(entry, dict):
            _claim(entry.get("name"), None, None)

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
            _claim(entry.get("name"), entry.get("version"), mtime)

    return index


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
    return _stamp(detail, published_index().get(name))


def attach_updates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put ``update`` on every row in *rows*, reading the caches once.

    Every host surface must answer the same way. An app's banner is written
    once and mounts both as a routed page and as a side-panel tab, and the two
    read their row from different endpoints -- so a field present on one and
    absent on the other would have the app claim nothing is known about itself
    on half its surfaces, silently, because absent MEANS unknown.

    Keyed by each row's own ``name``; a row without one is left untouched.
    """
    index = published_index()
    for row in rows:
        name = row.get("name")
        if isinstance(name, str) and name:
            _stamp(row, index.get(name))
    return rows
