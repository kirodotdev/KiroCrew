"""Read what an index-tier registry's branch publishes now, for the apps installed from it.

A registry whose index rows carry no ``version`` leaves the version to each app's
own ``app.json``. For an installed app the listing reads that file from the
installed checkout and caches it apart (``_installed_manifest_cache_path``), so it
names the installed version. The SOURCE manifest cache is what ``update_status``,
the store listing's ``version`` and the automatic update read as what the branch
publishes, and for an installed app only the owner-tier prewarm writes it. An
index-tier registry has no prewarm, and ``refresh_registries`` expires the source
cache on every refresh, so an app installed from such a registry was never shown,
or automatically given, a newer release.

:func:`refresh_installed_tips` runs after an index-tier registry's index was
fetched: by ``refresh_registries``, and by the listing's own index fetch
(``_load_external_registries``) whenever its cached index lapsed. For each row of
that FRESH index that carries no publishable version, names an app installed from
that row (the same registry and clone URL the install recorded) and has no fresh
source manifest cache, it reads ``app.json`` from the branch tip with one shallow
clone and writes it as the row's source manifest cache. The clone
is ``_fetch_app_manifest``'s own throwaway clone, made as the listing makes it for
that row: anonymous git environment and strict sandbox, behind the trusted-host
check, except for a row in the configured registry repository itself, which takes
the same-repository carve-out. Passing no app name skips the installed checkout,
which is the copy this exists to look past.

Advisory, like every reader of these caches: the file decides what a surface
shows. An update re-resolves its source through ``_resolve_install_entry``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline import caches as _caches
from kiro_crew.apps.registry_pipeline import git_targets as _git_targets
from kiro_crew.apps.registry_pipeline import manifests as _manifests
from kiro_crew.apps.registry_pipeline import sources as _sources

logger = logging.getLogger(_FACADE)

#: Most rows one batch reads a tip for, per registry. Only installed apps whose
#: row has no version qualify, so a real registry stays far below it; the cap keeps
#: an index that lists one app many times from turning a refresh or a listing into
#: a crawl. Rows past it, in index order, are not read by that batch. A later
#: listing batch reaches them only while the rows before them still hold fresh
#: caches, and a refresh, which expires every cache first, never does. Until one is
#: read its app shows no newer release, and an update the user runs still installs
#: the branch tip.
MAX_ROWS = 20

#: Tip clones one batch runs at once.
CONCURRENCY = 4

#: Seconds one registry's batch may take. A clone still running at the deadline is
#: cancelled, which kills its process tree and removes its temporary directory. Its
#: row keeps the cache that lapsed or that the refresh expired, so its app shows no
#: newer release until a later batch reads the tip in time.
BATCH_BUDGET_SECS = 30.0


def _rows_to_read(
    reg: Any, entries: list[dict[str, Any]], installed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The rows of *entries* whose tip :func:`refresh_installed_tips` reads.

    A row qualifies when it is this registry's (``_registry`` is its public
    name), it carries no publishable version, an installed app recorded this
    row as its source (same name, ``registry:`` source, same ``sourceRegistry``
    or another build-pinned one, and the same repository as the row's clone
    URL), and its source manifest cache is not fresh. A fresh one already
    answers the listing and ``update_status`` with a tip read within the
    cache's life, so the
    listing's hourly index fetch clones each tip at most once per manifest TTL.
    ``refresh_registries`` expires that cache first, so an explicit refresh
    reads every row that otherwise qualifies. Reads the cache files, so it
    runs off the loop.
    """
    from kiro_crew.apps.registry import is_registry_source
    from kiro_crew.apps.update_status import publishable_version

    registry_name = _git_targets._public_registry_name(reg)
    sources: dict[str, list[tuple[str, str]]] = {}
    for record in installed:
        if not isinstance(record, dict):
            continue
        name, source, url = record.get("name"), record.get("source"), record.get("sourceUrl")
        if not (isinstance(name, str) and isinstance(source, str) and isinstance(url, str)):
            continue
        if not is_registry_source(source) or not url:
            continue
        registry = record.get("sourceRegistry")
        sources.setdefault(name, []).append((url, registry if isinstance(registry, str) else ""))

    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("_registry") != registry_name:
            continue
        name = entry.get("name")
        if not isinstance(name, str) or publishable_version(entry.get("version")):
            continue
        git_url = _git_targets._entry_git_url(entry)
        if not any(
            _git_targets._same_git_target(git_url, url)
            and (
                registry == registry_name
                or _git_targets._same_git_target(registry, registry_name)
                or _sources._is_pinned_registry_move(registry, registry_name)
            )
            for url, registry in sources.get(name, [])
        ):
            continue
        # The same test ``_resolve_manifest`` applies before it reads anything else.
        if _caches._read_manifest_cache(entry):
            continue
        if len(rows) >= MAX_ROWS:
            logger.warning(
                "installed app tips: registry %r has more than %d installed rows "
                "without a version; the rows past them are not read, so their "
                "apps show no newer release",
                registry_name,
                MAX_ROWS,
            )
            break
        rows.append(entry)
    return rows


async def _read_tip(entry: dict[str, Any], gate: asyncio.Semaphore, written: list[str]) -> None:
    """Fetch *entry*'s branch-tip ``app.json`` and cache it as the source manifest.

    Appends the row's name to *written* once a manifest naming the row's app is
    on disk. A fetch that fails, or returns a manifest for another app, writes
    nothing.

    The clone is the one the listing's own fetch (``_resolve_manifest``) makes
    for the row, so the tip is the release an update of this row installs:

    - No ``commit``. Every row read here carries ``_registry``, so none is a
      catalog row, and ``install_from_registry`` drops a non-catalog row's pin
      and installs the branch tip.
    - The same-repository carve-out. A row whose clone URL is the configured
      registry repository itself clones with that registry's configured target
      and the owner's credentials (``_owner_designated_repo_target``), as the
      listing and the install do. Every other row clones anonymously.
    """
    branch = entry.get("branch", "main")
    subdirectory = entry.get("subdirectory", "")
    # Off the loop: it reads the configured registries.
    owner_target = await asyncio.to_thread(_sources._owner_designated_repo_target, entry)
    async with gate:
        manifest = await _manifests._fetch_app_manifest(
            entry.get("repo", ""),
            branch if isinstance(branch, str) and branch else "main",
            subdirectory if isinstance(subdirectory, str) else "",
            # No app name: the installed checkout is the copy this reads past.
            app_name="",
            git_url=owner_target or _git_targets._entry_git_url(entry),
            owner_designated=bool(owner_target),
        )
    if not isinstance(manifest, dict) or manifest.get("name") != entry.get("name"):
        return
    if await asyncio.to_thread(_caches._write_manifest_cache, entry, manifest):
        written.append(str(entry.get("name", "")))


async def refresh_installed_tips(reg: Any, entries: list[dict[str, Any]]) -> int:
    """Cache the branch-tip ``app.json`` of the apps installed from *reg*'s rows.

    *entries* MUST be the index a fetch of *reg* just returned, never a cached
    one. ``refresh_registries`` calls this after it expired the rows' manifest
    caches: written before that expiry, the manifests would be expired with the
    rest. The listing's index fetch calls it before the rows resolve. Any
    registry tier but the index tier returns 0 at once, because the owner-tier
    prewarm already reads the tip of an installed app's row, with the owner's
    credentials. Never raises: a refresh or a listing must still answer when a
    tip cannot be read.

    Returns the number of manifests written.
    """
    if not entries:
        return 0
    try:
        tier = await asyncio.to_thread(_sources._registry_trust_tier_of, reg)
    except Exception:  # noqa: BLE001 - an unreadable tier reads nothing, never fails a refresh
        logger.warning("installed app tips: could not read a registry's trust tier", exc_info=True)
        return 0
    if tier != _sources._TRUST_INDEX:
        return 0
    from kiro_crew.apps.manager import list_apps

    try:
        installed = await asyncio.to_thread(list_apps)
    except Exception:  # noqa: BLE001 - see above
        logger.warning("installed app tips: could not list installed apps", exc_info=True)
        return 0
    rows = await asyncio.to_thread(_rows_to_read, reg, entries, installed)
    if not rows:
        return 0
    gate = asyncio.Semaphore(CONCURRENCY)
    # A list, not a rebound counter, so the count is right when the deadline
    # cancels the reads still in flight.
    written: list[str] = []
    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                *[_read_tip(row, gate, written) for row in rows], return_exceptions=True
            ),
            timeout=BATCH_BUDGET_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "installed app tips: registry %r did not finish %d tip read(s) within %.0fs",
            _git_targets._public_registry_name(reg),
            len(rows),
            BATCH_BUDGET_SECS,
        )
        return len(written)
    for row, outcome in zip(rows, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(
                "installed app tips: reading the tip of %r failed: %s",
                row.get("name", ""),
                type(outcome).__name__,
            )
    return len(written)
