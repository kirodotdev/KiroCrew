"""The store listing, and the lookups install and consent resolve through.

Seed, published catalog and external registries merged under one precedence
(``list_registry``, ``list_catalog_apps``); install status and the server-computed
trust fields; the refresh sweep; and the candidate resolution that decides which
row an install or an update acts on.
"""

from __future__ import annotations

import asyncio
import logging
import platform as _platform
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps import official_catalog
from kiro_crew.apps.execution import app_execution_denied
from kiro_crew.apps.manager import get_app
from kiro_crew.apps.manager import list_apps as list_installed_apps
from kiro_crew.apps.manager import shipped_builtin_names
from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline.caches import (
    _expire_cache_file,
    _external_registry_cache_identity,
    _manifest_cache_path,
    _read_external_registry_cache,
)
from kiro_crew.apps.registry_pipeline.checkout import _communicate_with_timeout
from kiro_crew.apps.registry_pipeline.git_targets import (
    _entry_git_url,
    _git_target_is_unsupported,
    _normalize_git_target,
    _public_registry_name,
    _same_git_target,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.indexes import (
    _apply_configured_branch,
    _fetch_and_cache_external_registry,
    _load_external_registries,
)
from kiro_crew.apps.registry_pipeline.manifests import _resolve_manifest
from kiro_crew.apps.registry_pipeline.sources import _effective_registries, _load_registry_file
from kiro_crew.apps.registry_pipeline.subprocess_env import _detect_probe_env
from kiro_crew.sandbox import (
    create_subprocess_limited,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)

logger = logging.getLogger(_FACADE)


# Source type prefix for registry-installed apps.
SOURCE_REGISTRY_PREFIX = "registry:"


def _catalog_row_supersedes_seed(seed: dict[str, Any], catalog_row: dict[str, Any]) -> bool:
    """Whether *catalog_row* may stand in for the same-named *seed* row.

    The catalog is the shelf and the bundled seed is its offline snapshot, so when
    both describe the same app the catalog's row is the better one: it carries the
    curated copy AND the commit pin, while the seed carries four coordinate fields
    and no pin. Deferring to the seed instead is what made the pin dead data for
    every app that actually has one -- both git catalog entries are also seed
    entries, so a name-collision rule that favoured the seed discarded 100% of the
    published pins.

    Requiring URL equality is the security half. Without it, a catalog revision
    could rebind a bundled app's NAME to a different repository, and app trust is
    keyed by name -- so the row that replaces the seed must be describing the same
    repository the wheel shipped against, not merely claiming the same name.
    """
    return _same_git_target(_entry_git_url(seed), _entry_git_url(catalog_row))


def _is_catalog_row(entry: dict[str, Any]) -> bool:
    """Whether *entry* came from the official catalog and may skip the manifest fetch.

    Both halves are required. ``_catalog`` is on the row-projection allowlist, so
    an external registry's index -- untrusted, index-controlled JSON -- can set it
    on its own rows; ``_registry`` is attached server-side per configured registry
    and cannot be forged. Testing only ``_catalog`` would let such a row skip the
    fetch of the app's OWN manifest and keep index-supplied display copy instead,
    which is the substitution the manifest fetch exists to prevent.
    """
    return bool(entry.get("_catalog")) and not entry.get("_registry")


async def _identity(entry: dict[str, Any]) -> dict[str, Any]:
    """Return *entry* unchanged, as an awaitable.

    Lets the manifest-fetch gather hold a uniform list of coroutines while a
    catalog row skips the fetch entirely: the alternative is branching on row
    kind at the gather site AND at the result-zip below it, where an index
    mismatch would silently pair a row with another row's manifest.
    """
    return entry


def _is_external_row(entry: dict[str, Any]) -> bool:
    """Whether *entry* is an EXTERNAL registry's row, for trust-field stamping.

    Refuses on the PRESENCE of an external marker rather than granting from its
    absence: ``_registry`` is attached server-side per configured registry and
    cannot be forged by index content, and ``provenance == "external"`` is the
    server-computed stamp derived from it. Deliberately NOT
    :func:`_remote_controlled_url`: a ``_catalog`` row is remote-controlled for
    CREDENTIAL purposes but is still an app WE list, so its trust fields are
    first-party.
    """
    return bool(entry.get("_registry")) or entry.get("provenance") == "external"


def _enrich_with_install_status(
    entries: list[dict[str, Any]],
    installed_map: dict[str, dict[str, Any]],
    detected: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Add ``installed``, ``installedVersion``, ``enabled``, ``updateAvailable``.

    *detected* is a set of app names that were found via ``detectInstalled``
    shell commands (installed outside Kiro Crew's app manager).
    """
    detected = detected or set()
    for entry in entries:
        name = entry.get("name", "")
        existing = installed_map.get(name)
        externally_detected = name in detected

        entry["installed"] = existing is not None or externally_detected
        if existing:
            entry["installedVersion"] = existing.get("version", "")
            entry["enabled"] = existing.get("enabled", False)
            # ``origin`` is trust-adjacent (surfaces read ``"builtin"`` as
            # first-party), and ``installed_map`` matches by NAME alone — so an
            # external registry's row named after an installed built-in must not
            # inherit that app's ``origin``, or the gateway emits a row whose
            # ``origin`` contradicts the ``provenance: "external"`` stamped
            # beside it by ``_apply_trust_fields``. External rows keep whatever
            # the trust boundary decides for them instead.
            if not _is_external_row(entry):
                entry["origin"] = existing.get("origin", "registry")
            entry["resources"] = existing.get("resources", "gateway")
            entry["lifecycle"] = existing.get("lifecycle", "gateway")
            entry["updateAvailable"] = _version_newer(
                entry.get("version", ""),
                existing.get("version", ""),
            )
        elif externally_detected:
            entry["installedVersion"] = "unknown"
            entry["enabled"] = True
            entry["origin"] = "external"
            entry["resources"] = "app"
            entry["lifecycle"] = "app"
            entry["updateAvailable"] = False
        else:
            entry["updateAvailable"] = False
    return entries


#: Index-declared author spellings that name US, folded by ``_fold_author``.
#
# The product name is two words, so the bundled catalog and the official
# published catalog both state ``Kiro Crew``; the historical bundled spelling
# was the single token ``kirocrew``. Both are us, so both mint the mark.
FIRST_PARTY_AUTHORS: frozenset[str] = frozenset(
    {"kirocrew", "kiro crew"}  # brand-ok: folded values, lower-cased by contract
)


def _fold_author(value: object) -> str:
    """Fold an author name for the first-party comparison.

    NFKC maps fullwidth forms onto ASCII, category-``Cf`` code points (ZWSP,
    soft hyphen, bidi marks) are dropped, and runs of whitespace collapse to a
    single space. Without this, ``Ｋｉｒｏ Ｃｒｅｗ`` and ``kiro\u200bcrew``
    read as us to a human but compare unequal, so an index row that legitimately
    names us in a non-ASCII form would silently lose the mark.

    Widening the match is safe HERE and only here: ``_apply_trust_fields``
    short-circuits every ``_registry``-tagged row to ``verified: False`` before
    consulting the author at all, so the folded comparison is only ever reached
    for rows whose index we ship or sign. Do not reuse this to GRANT trust on a
    path where untrusted content supplies the name.
    """
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKC", value)
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return " ".join(folded.split()).lower()


def _apply_trust_fields(
    entries: list[dict[str, Any]],
    *,
    trust_repositories: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Stamp server-computed trust fields on every row.

    SECURITY CONTRACT: these fields are the API trust boundary for
    ``GET /api/apps/registry``. They are computed here — where the
    server-attached ``_registry`` tag is authoritative — and OVERWRITE any
    value an index entry may have published, so an external registry can
    never spoof them. Client code must read these fields and must not
    re-derive trust from the absence of ``_registry``, an internal tagging
    detail. ``_registry`` stays in the payload: the external-source label
    text, older clients, ``appManifest.ts::keysFor`` (first-party copy
    gate), and ``pickFeatured``'s legacy arm all still read it — do not
    stop emitting or rename it without migrating those dependants.

    Per row:

    - ``provenance``: ``"external"`` when ``_registry`` is set (the tag is
      applied server-side per configured registry and cannot be forged by
      index content); otherwise ``"builtin"`` when ``origin == "builtin"``,
      else ``"official"``.

      ``"official"`` means "an app WE list", and the bundled
      ``app-registry.json`` is one delivery of that list — the offline seed
      that ships inside the wheel. It answers the same question the remote
      signed catalog answers, so it gets the same value rather than a second
      one: two provenance values for one claim would put a weaker integrity
      guarantee (rides on the install artifact, cannot be revoked before the
      next release) behind a label a client cannot tell apart from the
      stronger one. The value names WHOSE list an app is on; how that list
      reached the client is a separate axis, and belongs in a separate field
      once there is more than one answer to record.

      ``"core"`` was the previous spelling. Clients accept both during the
      migration, so an older gateway's rows still label correctly.
    - ``verified``: ``True`` only when provenance is NOT ``"external"`` AND
      (``origin == "builtin"`` or the INDEX-declared author — snapshotted
      into ``_index_author`` by ``list_registry`` before the manifest merge
      — names us after ``_fold_author`` (see ``FIRST_PARTY_AUTHORS``). The
      badge asserts first-party
      provenance next to an Install button that runs setup code with
      gateway privileges, so it is never awardable from index-published
      trust keys or from the repo-fetched ``app.json``: a third-party core
      repo publishing ``"author": "kirocrew"`` in its manifest does not
      mint it (the merged ``author`` display field is deliberately NOT
      consulted).
    - ``featured``: dropped entirely from external rows so an external index
      can never self-flag into the Discover spotlight, regardless of client
      logic. Core-entry ``featured`` flags are preserved.
    - ``origin``: on external rows, any value other than the server-stamped
      ``"external"`` is dropped, so the wire never carries an ``origin`` that
      contradicts ``provenance: "external"`` — neither an index-published one
      nor one cross-stamped from an installed same-named app.
    - ``trustRepository``: the normalized clone target the server resolved for
      the app. It is OVERWRITTEN here, never copied from index or manifest
      content, because the consent modal sends it back as proof of what the
      operator reviewed. ``trust_repositories`` lets the catalog storefront
      supply coordinates resolved separately from its display-only rows.
    """
    for entry in entries:
        entry.pop("trustRepository", None)
        name = entry.get("name")
        if trust_repositories is None:
            trust_candidate = _entry_git_url(entry)
        elif isinstance(name, str):
            trust_candidate = trust_repositories.get(name, "")
        else:
            trust_candidate = ""
        # A semantic query cannot be removed from the consent identity while
        # remaining on the eventual Git transport. Do not mint a grant proof for
        # a target the install path must refuse.
        trust_repository = (
            ""
            if _git_target_is_unsupported(trust_candidate)
            else _normalize_git_target(trust_candidate)
        )
        if trust_repository:
            entry["trustRepository"] = trust_repository

        # These coordinates are display/provenance fields on the storefront
        # response. Installation resolves its own authoritative row again; the
        # browser neither needs nor may receive embedded clone credentials.
        for coordinate_key in ("gitUrl", "repo", "sourceUrl"):
            coordinate = entry.get(coordinate_key)
            if isinstance(coordinate, str):
                entry[coordinate_key] = _strip_git_target_userinfo(coordinate)

        registry_name = entry.get("_registry")
        if isinstance(registry_name, str):
            entry["_registry"] = _strip_git_target_userinfo(registry_name)

        # ``stargazersCount`` is a trust cue, so it follows the ``featured``
        # precedent below: only the publisher's bake step may mint it, and an
        # external index can never self-report one (a fabricated ``★ 50K`` on
        # a hostile git app would render identically to a signed-catalog
        # count — a false trust cue is worse than none). The shape check
        # still runs on EVERY row because this function is the only boundary
        # every row crosses (``_resolve_manifest`` returns the row unchanged
        # when the manifest fetch fails, so the allowlist projection is not a
        # guaranteed exit). ``bool`` is excluded (it IS an int subclass), and
        # the JS safe-integer bound is a layout guard: Python accepts a
        # 309-digit int that JavaScript renders as hundreds of digits.
        stars = entry.get("stargazersCount")
        if (
            not isinstance(stars, int)
            or isinstance(stars, bool)
            or stars < 0
            or stars > official_catalog._STARS_MAX
        ):
            entry.pop("stargazersCount", None)

        index_author = entry.pop("_index_author", None)
        folded_author = _fold_author(index_author)
        if entry.get("_registry"):
            entry["provenance"] = "external"
            entry["verified"] = False
            entry.pop("featured", None)
            entry.pop("stargazersCount", None)
            # ``origin`` is trust-adjacent (``"builtin"`` reads as first-party
            # to every consumer), and on an external row it can arrive from
            # untrusted content: an index may publish the key itself, and it
            # survives a failed manifest fetch because ``_resolve_manifest``
            # returns the row as-is on that path. The only value the server
            # itself stamps on an external row is ``"external"``
            # (``detectInstalled`` hits in ``_enrich_with_install_status``);
            # anything else must not go on the wire beside
            # ``provenance: "external"``. Scrubbed HERE and not only at the
            # sources because this function is the trust boundary — a rule
            # stated anywhere else is a rule some later assignment can undo.
            if entry.get("origin") != "external":
                entry.pop("origin", None)
        else:
            builtin = entry.get("origin") == "builtin"
            entry["provenance"] = "builtin" if builtin else "official"
            if entry.get("_catalog"):
                # A catalog row's author is curated copy from a document whose
                # signature this client does not yet check, so it must never mint
                # the first-party badge.
                #
                # The refusal lives HERE and not at the row's source because
                # omitting `_index_author` upstream does not survive: the snapshot
                # loop in `list_registry` assigns `_index_author = entry["author"]`
                # unconditionally, which silently re-created the very path the
                # omission was meant to close. This function is the trust
                # boundary, so a rule stated anywhere else is a rule some later
                # assignment can undo.
                entry["verified"] = False
            else:
                entry["verified"] = builtin or folded_author in FIRST_PARTY_AUTHORS
    return entries


def _trust_repository_bindings(
    entries: list[dict[str, Any]],
    installed_map: dict[str, dict[str, Any]],
    resolved: dict[str, str] | None = None,
) -> dict[str, str]:
    """Authoritative consent target for each storefront row.

    An installed app is bound to the source URL recorded at install time; that
    is also what the grant handler resolves first. A not-yet-installed catalog
    row may have display and install coordinates from separate documents, so its
    caller supplies the freshly resolved target in *resolved*. Every remaining
    row (seed and external registries) resolves through ``_entry_git_url`` so a
    legitimate ``gitUrl``/``repo`` difference follows the same precedence as
    clone/install rather than treating the display alias as authority.
    """
    bindings: dict[str, str] = {}
    resolved = resolved or {}
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        installed = installed_map.get(name)
        if installed is not None:
            # The listing row was resolved from the same authoritative source
            # as install. Supply it to the shared legacy fallback so storefront
            # proof and the grant handler cannot disagree, without performing a
            # second blocking catalog lookup on the event loop.
            coordinate = resolved.get(name)
            authoritative_entry = {"gitUrl": coordinate} if coordinate is not None else entry
            _, bindings[name] = resolve_installed_trust_repository(
                installed, registry_entry=authoritative_entry
            )
        elif name in resolved:
            bindings[name] = resolved[name]
        else:
            bindings[name] = _entry_git_url(entry)
    return bindings


def _version_newer(registry_ver: str, installed_ver: str) -> bool:
    """Return True if registry version is strictly newer than installed.

    Compares semver-style version strings (major.minor.patch).
    Pre-release suffixes (e.g. ``-beta.1``) and build metadata
    (e.g. ``+build.123``) are stripped before comparison.
    Falls back to False if parsing fails (conservative).
    """

    def _parse(v: str) -> tuple[int, ...]:
        # Strip pre-release and build metadata: "1.2.3-beta.1+build" → "1.2.3"
        base = v.split("-", 1)[0].split("+", 1)[0]
        parts = [int(x) for x in base.split(".")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)

    try:
        return _parse(registry_ver) > _parse(installed_ver)
    except (ValueError, AttributeError):
        return False  # Conservative: don't flag update on parse failure


async def refresh_registries(repo: str | None = None) -> dict[str, Any]:
    """Refetch external-registry caches (fetch-then-swap) and re-warm.

    For every configured registry (or just the one whose ``.repo`` matches
    *repo*), refetches its index and — only on a successful fetch — overwrites
    the cache and expires the per-app manifest caches its entries contributed
    (via mtime backdating, so a failed manifest refetch still falls back to the
    stale copy). A registry whose refetch FAILS keeps its existing cache intact
    and is reported in ``failed`` rather than silently reported as synced.

    Returns ``{ok, refreshed, failed, results, apps, lastSyncedAt}`` where
    ``ok`` is True only if every matched registry refreshed successfully and
    ``results`` carries the per-registry outcome so the UI can distinguish
    "synced" from "sync failed, serving stale". When *repo* is supplied but
    matches no configured registry, returns ``ok: False`` with
    ``not_found: True`` so the route can map it to HTTP 404.
    """
    registries = await asyncio.to_thread(_effective_registries)
    if repo:
        registries = [r for r in registries if _same_git_target(r.repo, repo)]
        # A caller-supplied ``repo`` that matches no configured registry is a
        # client error, not a silent success: refreshing nothing and returning
        # ``ok: true`` would let an API client believe a sync happened when the
        # target does not exist. Signal not-found so the route maps it to 404.
        if not registries:
            return {
                "ok": False,
                "not_found": True,
                "refreshed": [],
                "failed": [],
                "results": [],
                "apps": 0,
                "lastSyncedAt": datetime.now(timezone.utc).isoformat(),
            }

    refreshed: list[str] = []
    failed: list[str] = []
    results: list[dict[str, Any]] = []
    for reg in registries:
        name = _external_registry_cache_identity(reg)
        display_name = _public_registry_name(reg)
        # Read the (possibly stale) prior index up front so we know which
        # per-app manifest caches this registry contributed, even if the
        # refetch changes/removes some entries.
        prior = await asyncio.to_thread(_read_external_registry_cache, name, ignore_ttl=True)
        # Fetch-then-swap: the cache is overwritten only on a successful fetch.
        entries = await _fetch_and_cache_external_registry(reg)
        if entries is None:
            failed.append(display_name)
            results.append({"name": display_name, "ok": False})
            continue
        # Expire per-app manifest caches so fresh display info is refetched
        # lazily on the next read (mtime expiry preserves the stale fallback).
        # Both the prior and the fresh index rows contribute: the cache path is
        # derived from each row's FULL source coordinates, so a row whose
        # branch/repo changed in the new index expires the old coordinates'
        # cache (via the prior row) as well as priming a miss for the new ones.
        expire_paths: set[Path] = set()
        for e in (prior or []) + entries:
            if not isinstance(e, dict):
                continue
            entry_name = e.get("name")
            if isinstance(entry_name, str) and entry_name:
                expire_paths.add(_manifest_cache_path(e))
        for cache_path in expire_paths:
            await asyncio.to_thread(_expire_cache_file, cache_path)
        refreshed.append(display_name)
        results.append({"name": display_name, "ok": True})

    # Re-warm so the response's app count reflects post-refresh state (and
    # untouched registries read their still-valid caches).
    apps = await list_registry()

    return {
        "ok": not failed,
        "refreshed": refreshed,
        "failed": failed,
        "results": results,
        "apps": len(apps),
        "lastSyncedAt": datetime.now(timezone.utc).isoformat(),
    }


async def _detect_installed_probe(
    entries: list[dict[str, Any]],
    installed_map: dict[str, dict[str, Any]],
) -> set[str]:
    """Run each entry's ``detectInstalled`` probe; return the names that report installed.

    Names already known to the app manager (present in *installed_map*) are
    skipped, as are names whose execution policy denies the probe. A probe
    timeout or ``OSError`` is swallowed and treated as not-installed. Shared by
    ``list_registry`` (offline path) and ``_append_external_registry_apps``
    (online path) so both probe identically -- an app installed OUTSIDE the app
    manager reads installed on either path.
    """
    detected: set[str] = set()
    for entry in entries:
        name = entry.get("name", "")
        if name in installed_map:
            continue  # already known, skip detection
        detect_cmd = entry.get("detectInstalled", "")
        if not detect_cmd:
            continue
        denied = app_execution_denied(
            name,
            action="registry_detect_installed",
            caller="registry",
            repository=_entry_git_url(entry),
        )
        if denied:
            logger.debug("Skipping registry detectInstalled for %s: %s", name, denied)
            continue
        try:

            base_cmd = ["/bin/sh", "-c", detect_cmd]
            # Through the single sandboxed-spawn chokepoint, not a hand-rolled
            # wrap + cgroup pair: it applies the strict launcher, the credential
            # scrub and the cgroup DoS ceiling, AND it forwards the systemd bus
            # locators that the ceiling's own `systemd-run --user` wrapper needs
            # to reach the user bus, dropping them again with an `env -u` shim
            # inside the scope so the probe itself never sees a live bus address.
            # A caller-built env that omits those locators makes `systemd-run`
            # exit 1 before the command runs, which with DEVNULL stderr reads as
            # "not installed" for every app on a cgroup-delegated host.
            #
            # `_detect_probe_env` is the credential-free base it scrubs on top of:
            # no agent socket, no git credential helper, no prompt, no toolchain
            # variable. The command string comes from a registry manifest, which
            # is untrusted content, and `strict` mode's own scrub only runs when
            # the launcher does -- not on Windows, and not on a host with no
            # sandbox backend plus agent.sandbox_allow_unsandboxed_exec -- so the
            # env handed over here is the only control left on those hosts.
            sandboxed_cmd, probe_env, _cleanup = await sandboxed_spawn_argv_async(
                base_cmd,
                mode="strict",
                env=_detect_probe_env(),
                _prepare=sandboxed_spawn_argv,
            )
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=probe_env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            await _communicate_with_timeout(proc, timeout=5)
            if proc.returncode == 0:
                detected.add(name)
                logger.info("Detected external install: %s", name)
        except (asyncio.TimeoutError, OSError):
            pass  # detection failed, treat as not installed
    return detected


async def _append_external_registry_apps(
    rows: list[dict[str, Any]],
    reserved_names: set[Any],
    installed_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Append user-configured external-registry apps to *rows*; return ``(rows, detected)``.

    The SINGLE site where external registries merge into a store listing, called
    by both ``list_registry`` (offline fallback) and ``list_catalog_apps`` (online
    catalog path) so the two paths cannot drift. External rows:

    - load via ``_load_external_registries`` (each server-tagged ``_registry``);
    - deduplicate by ``name`` against *reserved_names* AND each other, so a
      catalog/seed/builtin row always wins a collision and an external row only
      ADDS a name no reserved source claims (mirrors ``list_registry``'s original
      ``seen_names`` precedence). The caller reserves EVERY name the catalog and
      seed declare -- including a catalog ``git`` name it filtered out as
      not-yet-installable -- so an external row can never shadow a name install
      resolves by, which would point install-by-name at the wrong repository;
    - resolve display copy from the app's own ``app.json`` via ``_resolve_manifest``
      (the per-app fetch external rows pay today; catalog/seed rows do not pay it);
    - are probed with ``detectInstalled`` via ``_detect_installed_probe``.

    ``_index_author`` is deliberately NOT snapshotted here: every external row
    carries ``_registry``, so ``_apply_trust_fields`` takes its external branch,
    which drops ``_index_author`` and never derives the verified mark from it.
    """
    external = await _load_external_registries()
    seen = set(reserved_names)
    kept: list[dict[str, Any]] = []
    for entry in external:
        name = entry.get("name")
        if name in seen:
            continue
        seen.add(name)
        kept.append(entry)
    if not kept:
        return rows, set()
    resolved = await asyncio.gather(
        *[_resolve_manifest(e) for e in kept],
        return_exceptions=True,
    )
    kept = [r if isinstance(r, dict) else kept[i] for i, r in enumerate(resolved)]
    detected = await _detect_installed_probe(kept, installed_map)
    rows.extend(kept)
    return rows, detected


async def list_registry() -> list[dict[str, Any]]:
    """Return all registry apps with display info and install status.

    1. Load minimal registry JSON (name, repo, branch)
    2. Load external registries from user config
    3. Fetch each app's app.json (cached, 24h TTL) for display info
    4. Run detectInstalled commands for external installs
    5. Enrich with install status from Kiro Crew's app manager
    6. Stamp server-computed trust fields (``provenance``/``verified``) and
       strip ``featured`` from external rows — see ``_apply_trust_fields``
    """
    entries = await asyncio.to_thread(_load_registry_file)

    # The catalog is the shelf, and the bundled seed is its offline snapshot.
    #
    # This runs FIRST, before the manifest fetches below, because a catalog row
    # already carries everything the list renders -- display copy, artwork, and
    # the `version` that decides whether an update is available. Applied here,
    # those rows need no per-app clone at all. Applied afterwards (which is what
    # `annotate` alone did) the clone is paid for and then overwritten.
    #
    # Seed rows LOSE a name collision when both name the same repository, because
    # the seed's four coordinate fields carry no pin: favouring it discarded every
    # published pin, since both git catalog entries are also seed entries. The seed
    # remains the fallback for when the catalog cannot be loaded at all (the
    # `except` below), which is the availability job it actually exists for. A
    # catalog row naming a DIFFERENT repository does not supersede -- see
    # `_catalog_row_supersedes_seed`.
    catalog_entries: list[dict[str, Any]] = []
    try:
        # A FRESH fetch, not `load_official_catalog` -- that reads the agent-writable
        # cache, and a cached row that MATERIALISES inventory can render with official
        # provenance and deduplicate the real same-named external row out of the
        # listing. The consent prompt would then describe an official app while the
        # name grant it produces installs the external one. The cache still feeds
        # `annotate` further down, which is display copy for rows that exist anyway
        # and skips rows carrying `_registry`.
        catalog_entries = await asyncio.to_thread(official_catalog.fetch_inventory_entries)
        if catalog_entries:
            by_name = {e.get("name"): i for i, e in enumerate(entries)}
            for row in official_catalog.inventory(catalog_entries):
                idx = by_name.get(row.get("name"))
                if idx is None:
                    entries.append(row)
                elif _catalog_row_supersedes_seed(entries[idx], row):
                    # Same app, same repo: take the catalog's row whole. It carries
                    # the pin and the curated copy; the seed carries neither.
                    # Replacing rather than overlaying `commit` onto the seed row is
                    # deliberate -- a row needs `_catalog` for the install path to
                    # honour its pin at all, and a seed row given `_catalog` would
                    # then skip the manifest fetch it depends on for display copy.
                    entries[idx] = row
                else:
                    logger.warning(
                        "catalog entry %r names a different repository than the "
                        "bundled seed (%r vs %r); keeping the seed row",
                        row.get("name"),
                        _entry_git_url(row),
                        _entry_git_url(entries[idx]),
                    )
    except Exception:  # noqa: BLE001 - degrade, never 500 the store
        # No catalog inventory this listing. That is the behaviour the store had
        # before this module existed (seed + built-ins discovered on disk), and it
        # is the right degradation: a name we cannot confirm right now must not
        # appear as an official row, because that row is what a consent grant is
        # made against.
        logger.warning("no official catalog inventory this listing", exc_info=True)

    # Load external registries from config, deduplicating against core and each other
    installed = await asyncio.to_thread(list_installed_apps)
    installed_map = {a["name"]: a for a in installed}
    # Snapshot the INDEX-declared author before the manifest merge below
    # overwrites ``author`` with the repo-fetched app.json value.
    # ``_apply_trust_fields`` derives ``verified`` from this snapshot only:
    # the bundled/edition index is trusted content, the fetched manifest is
    # the app author's — a repo publishing ``"author": "kirocrew"`` in its
    # app.json must not mint the badge. Unconditional assignment also
    # neutralizes an index that pre-seeds the key itself. External rows are
    # appended AFTER this by ``_append_external_registry_apps`` and always carry
    # ``_registry``, so ``_apply_trust_fields`` drops ``_index_author`` for them
    # and never reads it — which is why the helper does not snapshot it.
    for entry in entries:
        entry["_index_author"] = entry.get("author")

    # Fetch manifests in parallel, EXCEPT for catalog rows.
    #
    # A catalog row already carries the display fields and the `version` this
    # list needs, baked at publish time from the app's own app.json by a pipeline
    # that read it once, centrally. Cloning the app's repository again to learn
    # what the catalog already told us is the per-app network cost this document
    # exists to remove -- and it is O(N) in the number of third-party apps, which
    # is exactly the number the store is meant to grow.
    resolved = await asyncio.gather(
        *[_identity(e) if _is_catalog_row(e) else _resolve_manifest(e) for e in entries],
        return_exceptions=True,
    )
    entries = [r if isinstance(r, dict) else entries[i] for i, r in enumerate(resolved)]

    # Probe the seed/catalog rows, then append external registries at the single
    # shared merge site. Reserving every seed/catalog name means an external row
    # can only ADD a name none of them claim, which is the precedence this merge
    # site enforces.
    detected = await _detect_installed_probe(entries, installed_map)
    entries, external_detected = await _append_external_registry_apps(
        entries, {e.get("name") for e in entries}, installed_map
    )
    detected |= external_detected

    # Overlay the official catalog's curated fields LAST among the content
    # sources, so they win over a fetched manifest -- that is what curation
    # means: the catalog is ours, the manifest belongs to the app. It runs BEFORE
    # the trust stamp so `_apply_trust_fields` still derives `verified` from the
    # index-declared author snapshot, which the overlay deliberately leaves
    # alone while the document's signature is not yet checked.
    #
    # Containment, not defensiveness. This handler has no try/except above it, so
    # anything escaping the catalog step is an HTTP 500 for the WHOLE store -- and the
    # curated copy is an enhancement to a listing that is already complete without it.
    # "Anything went wrong, render what we had" is therefore the correct semantics at
    # this seam specifically, and a broad catch here is not hiding a defect: it logs
    # with a traceback, and the module's own precise guards still run first.
    #
    # Annotate from the SAME fresh entries the inventory came from, never the cache.
    #
    # Blocking the agent-writable cache from INTRODUCING a row is not enough; this
    # stops it from REWRITING one. `annotate` overlays `displayName` and `description`, which
    # are exactly what the consent modal renders, and it only skips rows carrying
    # `_registry` -- so a poisoned cache entry could re-label a freshly fetched
    # first-party row and the name-scoped grant would then execute the real app under
    # a spoofed identity. Same value, different verb, same trust boundary.
    #
    # When the fetch failed, `catalog_entries` is empty and no catalog-derived copy is
    # applied at all. That is the correct degradation: rows then render the copy their
    # own manifest supplies, which is what the store did before this module existed.
    # There is no third source to fall back to -- a second source for the same rows is
    # precisely the defect.
    if catalog_entries:
        try:
            official_catalog.annotate(entries, catalog_entries)
        except Exception:  # noqa: BLE001 - degrade, never 500 the store
            logger.warning("ignoring curated catalog copy after a failure", exc_info=True)

    trust_repositories = _trust_repository_bindings(entries, installed_map)
    return _apply_trust_fields(
        _enrich_with_install_status(entries, installed_map, detected),
        trust_repositories=trust_repositories,
    )


def _catalog_installable_rows() -> dict[str, dict[str, Any]]:
    """Catalog install rows by name, from a FRESH fetch -- never the cache.

    ``list_catalog_rows`` reads the cache under the data home, which is
    agent-writable. That is harmless while a cached row only re-dresses a row that
    exists anyway -- the posture ``annotate`` already documents -- and NOT harmless
    if a cached row could CREATE a listed row: a planted name would render with
    official provenance and deduplicate the real same-named external row out of the
    listing, so a consent prompt would describe an official app while the name grant
    it produces installs the external one.

    So the decision to LIST a catalog-only ``git`` name is authorised from the
    fetched document and never from the cache. ``fetch_inventory_entries`` is the
    only source allowed to materialise inventory, and it honours the module's
    failure memory, so an outage costs a refusal rather than a fresh timeout on
    every listing.

    Returns an empty mapping on ANY failure, which degrades the storefront to the
    seed's names -- the listing this path produced before the catalog could supply
    coordinates. Keeping the rows (rather than only their names) also lets the
    server show the exact resolved clone target in the consent dialog without a
    second network fetch.
    """
    try:
        entries = official_catalog.fetch_inventory_entries()
        return {
            row["name"]: row
            for row in official_catalog.inventory(entries)
            if isinstance(row.get("name"), str)
        }
    except Exception:  # noqa: BLE001 - degrade to the seed, never 500 the store
        logger.warning("cannot confirm the catalog's install coordinates", exc_info=True)
        return {}


async def list_catalog_apps() -> list[dict[str, Any]]:
    """Store rows built from the published catalog, enriched and trust-stamped.

    The JSON-only storefront path: when the published catalog is available its
    rows REPLACE the seed + per-app manifest fetch, so the store renders the
    published document's list and display copy. An empty result means the catalog
    was unavailable, and the caller falls back to ``list_registry`` offline.

    Install coordinates are the CATALOG's when it pins them: a ``git`` row is kept
    when the seed or an external registry names it, or when the catalog itself
    supplies validated pinned coordinates for it -- the same resolution
    ``inventory_for_install`` performs on the install path. Gating the listing on
    the seed alone made the two disagree, so a published app stayed invisible in
    the store until a release shipped a new seed -- the release-per-app cost
    ``inventory`` exists to remove. The row still carries no clone URL of its own;
    install resolves the coordinates by name. ``verified`` stays ``False`` for
    non-builtin rows until the catalog signature is checked, so this path never
    mints the first-party badge from a document trusted only as far as TLS.

    User-configured external registries (``config.registries``) are appended here
    too, through the same ``_append_external_registry_apps`` merge site
    ``list_registry`` uses, so they surface whether or not the catalog is
    reachable and are enriched, probed, and trust-stamped identically on both
    paths. A catalog/seed/builtin row WINS a name collision — external rows only
    ADD apps no catalog or seed name claims — and only external rows pay the
    per-app manifest fetch. The reserved names include EVERY catalog row name,
    snapshotted before the ``git``-installability filter below drops a
    not-yet-installable ``git`` row, so an external row can never shadow a name
    install resolves by (which would point install-by-name at the wrong repo).
    A ``builtin`` row naming a builtin this build cannot register is dropped the
    same way, and keeps its reservation the same way.
    """
    # Off the event loop: the first call after a cache expiry does network I/O.
    rows = await asyncio.to_thread(official_catalog.list_catalog_rows)
    if not rows:
        return []
    installable = await asyncio.to_thread(_load_registry_file)
    seed_by_name = {
        e["name"]: e for e in installable if isinstance(e, dict) and isinstance(e.get("name"), str)
    }
    installable_names = set(seed_by_name)
    # Reserve every catalog name BEFORE the git filter, plus every seed name, so
    # an external row can never shadow a catalog/seed name — including a catalog
    # `git` row filtered out here for not being installable yet, whose name
    # install still resolves by.
    reserved_names: set[Any] = {row.get("name") for row in rows} | installable_names
    # A `git` row the seed does not name is STILL installable when the catalog
    # pins it -- that is exactly what `inventory_for_install` resolves on the
    # install path. Asking only the seed made the two resolvers disagree: install
    # accepted a catalog-only row while the storefront dropped it, so a published
    # app was unlistable, and therefore undiscoverable, until a release shipped a
    # new seed.
    #
    # Only paid when it can change the answer. With every `git` row already seeded
    # the fetch cannot unlock anything, so the storefront's hot path keeps costing
    # one cached read.
    fresh_installable: dict[str, dict[str, Any]] = {}
    if any(
        row.get("source", {}).get("type") == "git" and row.get("name") not in installable_names
        for row in rows
    ):
        fresh_installable = await asyncio.to_thread(_catalog_installable_rows)
        installable_names |= set(fresh_installable)
    rows = [
        row
        for row in rows
        if row.get("source", {}).get("type") != "git" or row.get("name") in installable_names
    ]
    # A `builtin` row is only installable when this build ships that builtin, so a
    # catalog ahead of this gateway would otherwise render an Install that cannot work.
    if any(row.get("source", {}).get("type") == "builtin" for row in rows):
        shipped = await asyncio.to_thread(shipped_builtin_names)
        rows = [
            row
            for row in rows
            if row.get("source", {}).get("type") != "builtin" or row.get("name") in shipped
        ]

    # Catalog display rows intentionally carry no clone URL. Resolve the
    # consent target from the same install coordinates name-only install uses:
    # a bundled seed wins a different-repository catalog collision, while a
    # catalog-only row uses the freshly fetched pin row above.
    resolved_repositories: dict[str, str] = {}
    for row in rows:
        if row.get("source", {}).get("type") != "git":
            continue
        name = row.get("name")
        if not isinstance(name, str):
            continue
        install_row = seed_by_name.get(name) or fresh_installable.get(name)
        resolved_repositories[name] = _entry_git_url(install_row) if install_row is not None else ""

    installed = await asyncio.to_thread(list_installed_apps)
    installed_map = {a["name"]: a for a in installed}
    rows, detected = await _append_external_registry_apps(rows, reserved_names, installed_map)
    trust_repositories = _trust_repository_bindings(rows, installed_map, resolved_repositories)
    return _apply_trust_fields(
        _enrich_with_install_status(rows, installed_map, detected),
        trust_repositories=trust_repositories,
    )


def get_server_platform() -> dict[str, str]:
    """Return the server's platform info for frontend compatibility checks."""
    from kiro_crew.apps.manifest import PlatformConfig

    return {"os": PlatformConfig.current_os(), "arch": _platform.machine()}


def _seed_row(name: str) -> dict[str, Any] | None:
    """The bundled seed row named *name*, if this wheel shipped one."""
    for entry in _load_registry_file():
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _resolve_registry_row(name: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve *name* to an installable row, or a refusal reason.

    Searches the official catalog first, then the bundled seed, then external
    registry caches. Returns ``(row, reason)``; a non-empty *reason* means the
    caller must REFUSE and must not substitute another row.

    **"The catalog says there is no pin" and "I could not ask the catalog" are
    different answers, and collapsing them is a security defect.** A seeded
    official app has a published pin, so falling back to its branch-only seed row
    on a lookup failure installs a mutable branch tip while the store claims the
    app is pinned -- the pin silently not applying, which is this path's one
    quiet-yet-"successful" failure mode. So a lookup FAILURE with a seed row
    present refuses, while an authoritative "no catalog row" keeps using the seed.

    The availability cost is bounded and small: an install already requires the
    network to clone, so the only window this closes is "catalog host unreachable
    while the git host is reachable". Refusing there is the same choice made for
    the coordinate cache (never read for install) and for existing checkouts
    (never reused) -- a security property that holds only when the network
    cooperates is not one anybody can reason about.
    """
    seed_row = _seed_row(name)

    catalog_row: dict[str, Any] | None = None
    catalog_failed = False
    try:
        catalog_row = official_catalog.inventory_for_install(name)
    except official_catalog.CatalogUnavailable:
        catalog_failed = True
        # The caller may be classifying a config-derived grant name, and catalog
        # exceptions may include source coordinates.  Neither belongs in logs;
        # the fixed classification is enough to explain the fail-closed branch.
        logger.warning("official catalog coordinate lookup is unavailable")
    except Exception:  # noqa: BLE001 - fail closed: an unexpected error is not "no row"
        catalog_failed = True
        logger.warning("official catalog coordinate lookup failed unexpectedly")

    if catalog_row is not None and (
        seed_row is None or _catalog_row_supersedes_seed(seed_row, catalog_row)
    ):
        # The catalog row carries the pin and the curated copy; a same-repo seed row
        # carries neither, so it does not win. Deferring to it is what made every
        # published pin unreachable on the install path.
        return catalog_row, ""

    if catalog_failed:
        # Before ANY fallback, seed or external. Without the catalog we cannot know
        # whether this name is a catalog app, and app trust is keyed by name: a
        # same-named external registry row would install a different repository's
        # code under a name the owner already permitted for execution. The local
        # cache cannot be consulted to decide -- it is agent-writable, which is the
        # surface `inventory_for_install` refuses to read in the first place.
        detail = (
            "is bundled and may carry an official commit pin"
            if seed_row is not None
            else "may be an official catalog app"
        )
        return None, (
            f"the requested app {detail}, but the official catalog could not be "
            "reached to confirm it — refusing to resolve it from another source. "
            "Retry when the catalog is reachable."
        )

    if seed_row is not None:
        return seed_row, ""
    return _external_registry_row(name), ""


def get_registry_app(name: str) -> dict[str, Any] | None:
    """Look up a registry app by name (synchronous, for internal use).

    Returns the row, or ``None`` when no source offers *name*.

    RAISES :class:`official_catalog.CatalogUnavailable` when resolution was refused
    because the catalog could not be consulted. Raising rather than returning None is
    what lets the refusal keep its reason while this stays the single lookup the
    install path goes through: a returned None is indistinguishable from "no such
    app", and re-deriving the difference in the caller costs a second catalog fetch
    and stops being sound as soon as the refusal covers more than one case.

    Blocking (reads the bundled file, fetches the catalog over HTTPS, and reads
    external index caches) — call it off the event loop.
    """
    row, refusal = _resolve_registry_row(name)
    if refusal:
        raise official_catalog.CatalogUnavailable(refusal)
    return row


def resolve_installed_trust_repository(
    app: dict[str, Any],
    *,
    registry_entry: dict[str, Any] | None = None,
    allow_registry_lookup: bool = True,
) -> tuple[bool, str]:
    """Resolve the repository an installed app's trust prompt must bind to.

    New registry installs persist ``sourceUrl`` and are bound directly to that
    immutable install provenance.  Older installs predate that field, but retain
    the ``registry:<name>`` source marker; for those records, resolve the same
    current authoritative row a legacy update would use.  A genuinely local or
    self-registered app has neither form of registry provenance and remains a
    valid repository-less app.

    The boolean distinguishes that legitimate local case from a legacy registry
    record whose current source cannot be resolved.  Callers that grant execution
    trust must refuse the latter rather than silently creating a name-only grant.
    A caller that already resolved an authoritative storefront row can pass it as
    *registry_entry*, avoiding duplicate blocking catalog I/O while keeping the
    same coordinate precedence. ``CatalogUnavailable`` intentionally propagates
    when this function must perform the lookup itself, so a caller can fail closed.

    Runtime admission passes ``allow_registry_lookup=False``.  A legacy registry
    record without durable ``sourceUrl`` provenance is then unresolved instead of
    synchronously consulting the catalog from a request/startup event loop.  The
    storefront/grant path remains the only caller that may perform that blocking
    migration lookup, and already offloads it.
    """
    source_url = app.get("sourceUrl", "")
    source_url = source_url if isinstance(source_url, str) else ""
    if _git_target_is_unsupported(source_url):
        return False, ""
    repository = _normalize_git_target(source_url)
    if repository:
        return True, repository

    source = app.get("source", "")
    if not isinstance(source, str) or not is_registry_source(source):
        return True, ""

    name = app.get("name", "")
    if not isinstance(name, str) or not name:
        return False, ""
    if registry_entry is None and not allow_registry_lookup:
        return False, ""
    entry = registry_entry if registry_entry is not None else get_registry_app(name)
    if entry is None:
        return False, ""
    entry_url = _entry_git_url(entry)
    if _git_target_is_unsupported(entry_url):
        return False, ""
    repository = _normalize_git_target(entry_url)
    return bool(repository), repository


def _external_registry_row(name: str) -> dict[str, Any] | None:
    """The first row named *name* from an owner-configured external registry cache.

    Separate from the catalog/seed resolution above because these rows are a
    different trust class: they carry ``_registry``, which flips provenance and is
    attached here at the lookup boundary so a stale cache cannot omit it.
    """
    for reg in _effective_registries():
        cache_name = _external_registry_cache_identity(reg)
        public_name = _public_registry_name(reg)
        cached = _read_external_registry_cache(cache_name, ignore_ttl=True)
        if cached:
            for entry in cached:
                if entry.get("name") == name:
                    # Repair a stale cache's branch before install reads it.
                    _apply_configured_branch([entry], reg)
                    # Old cache files may predate persisted origin tags. Restore
                    # the authoritative discriminator at the lookup boundary so
                    # privacy gates never mistake a custom source for official.
                    return {**entry, "_registry": public_name}
    return None


def _registry_app_candidates(name: str) -> list[dict[str, Any]]:
    """Every catalog row named *name*: bundled first, then each configured
    registry in config order.

    :func:`get_registry_app` returns only the FIRST match, which is precisely
    what lets a same-named row from another source answer for an app installed
    from somewhere else.  Provenance-pinned resolution needs the full candidate
    set so it can select the row the app is actually pinned to.
    """
    candidates = [
        entry
        for entry in _load_registry_file()
        if isinstance(entry, dict) and entry.get("name") == name
    ]
    # The catalog is an inventory source, so it must appear here too. An app
    # installed from a catalog-only row records its provenance, and provenance-
    # pinned resolution then looks for a candidate offering that same source: a
    # candidate set that omits the catalog refuses EVERY later update of exactly
    # the apps this inventory exists to make installable.
    try:
        row = official_catalog.inventory_for_install(name)
        if row is not None:
            # REPLACE the equivalent seed candidates, do not merely outrank them.
            #
            # Ordering alone is not enough because provenance matching compares the
            # recorded source URL EXACTLY: an app installed when the seed's URL had no
            # `.git` suffix does not match a catalog URL that has one, so the match
            # walks past the catalog row and takes the retained seed -- delivering a
            # branch-tip update for an app the store presents as pinned. Ordering only
            # helped in the case that never needed help (URLs already identical).
            #
            # Replacing also keeps this path consistent with `_resolve_registry_row`,
            # where the catalog row wins outright; two resolvers disagreeing about the
            # same collision is how one of them ends up wrong.
            superseded = [c for c in candidates if _catalog_row_supersedes_seed(c, row)]
            if superseded:
                candidates = [c for c in candidates if c not in superseded]
                candidates.insert(0, row)
            else:
                candidates.append(row)
    except Exception:  # noqa: BLE001 - a failed lookup is not "no pin"; see below
        # A seed candidate names the SAME repository as the catalog row it shadows,
        # so provenance-pinned resolution would accept it and deliver a branch-tip
        # update for an app the store says is pinned. With the pin unknowable, the
        # seed candidates are dropped rather than offered.
        logger.warning(
            "official catalog lookup failed for %r; refusing to offer any candidate "
            "rather than resolving an update from an unconfirmed source",
            name,
            exc_info=True,
        )
        # Return immediately. Clearing the seed candidates and then falling through
        # to the external caches below still let a same-named external row answer --
        # and a tampered cache row missing its `_registry` marker reads as official,
        # so a provenance match would install an unpinned branch with the OWNER's
        # credentials. `_resolve_registry_row` already refuses before any fallback;
        # this is its sibling and must refuse the same way.
        return []
    for reg in _effective_registries():
        cached = _read_external_registry_cache(
            _external_registry_cache_identity(reg), ignore_ttl=True
        )
        for entry in cached or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                _apply_configured_branch([entry], reg)
                candidates.append(entry)
    return candidates


def _pinned_registry_entry(name: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    """Select the catalog row an installed app's recorded provenance pins it to.

    A row matches only when BOTH the clone URL and the originating registry id
    equal what was recorded at install time, so neither a row that reuses the
    name on a different repo nor a different registry publishing the same
    name/URL pair can stand in for the pinned source.  Returns None when no
    candidate matches.
    """
    want_url = str(meta.get("sourceUrl", "") or "")
    want_registry = str(meta.get("sourceRegistry", "") or "")
    for entry in _registry_app_candidates(name):
        # Credentials select how git fetches the source; they are not the source
        # identity. Rotation from one userinfo value to another must neither
        # rebind the grant nor strand an update of the same repository.
        if not _same_git_target(_entry_git_url(entry), want_url):
            continue
        current_registry = str(entry.get("_registry", "") or "")
        if current_registry != want_registry and not _same_git_target(
            current_registry, want_registry
        ):
            continue
        return entry
    return None


def _resolve_install_entry(name: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve the catalog row that ``install_from_registry`` may act on.

    Fresh installs — and legacy records that predate provenance capture, which
    carry only the bare ``registry:<name>`` marker — keep the historical
    first-match-wins :func:`get_registry_app` lookup, so no migration is needed
    and today's behaviour is unchanged for them.  An installed app that DOES
    carry provenance is pinned to it: its update must come from the source it was
    installed from, never from whichever same-named row happens to resolve first.

    Blocking (reads installed metadata, config, and index caches) — call it off
    the event loop.  Returns ``(entry, error)``; a non-empty *error* means the
    caller must refuse, and must NOT fall back to a bare-name lookup.
    """
    meta = get_app(name) or {}
    pinned_url = str(meta.get("sourceUrl", "") or "")
    if not pinned_url:
        try:
            return get_registry_app(name), ""
        except official_catalog.CatalogUnavailable as exc:
            # A refusal, not an absence: report the cause so the user is not sent
            # looking for a missing app during a catalog outage.
            return None, str(exc)
    entry = _pinned_registry_entry(name, meta)
    if entry is None:
        # ``pinned_url`` can contain clone credentials. It is comparison state,
        # never API/audit text: the caller returns and SEL-logs this reason.
        return None, (
            f"app {name!r} has no registry entry that matches its recorded source "
            "— refusing to update it from a different source"
        )
    return entry, ""


def _external_registry_app_by_repo(repo: str) -> dict[str, Any] | None:
    """Look up an app entry by repo across the user's external (federated)
    registries, reading local sync caches only (``ignore_ttl`` so a stale index
    still resolves) — never fetches, so it is safe to call from the per-request
    blob-proxy worker. Fails open to ``None``."""
    try:
        for reg in _effective_registries():
            cached = _read_external_registry_cache(
                _external_registry_cache_identity(reg), ignore_ttl=True
            )
            for entry in cached or []:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("repo"), str)
                    and _same_git_target(entry["repo"], repo)
                ):
                    _apply_configured_branch([entry], reg)
                    return entry
    except Exception:  # fail open: branch resolution must never break blob serving
        logger.debug("_external_registry_app_by_repo: read failed", exc_info=True)
    return None


def get_registry_app_by_repo(repo: str) -> dict[str, Any] | None:
    """Look up a registry app by repo name (for blob proxy branch lookup).

    Searches the bundled registry first, then the user's external (federated)
    registries — matching ``known_registry_repos()``'s union — so an
    external-registry app pinned to a non-``main`` branch resolves the correct
    ref in the ``/api/apps/blob`` branch fallback instead of silently 403ing.
    """
    for entry in _load_registry_file():
        if isinstance(entry.get("repo"), str) and _same_git_target(entry["repo"], repo):
            return entry
    return _external_registry_app_by_repo(repo)


def is_registry_source(source: str) -> bool:
    """Check if a source string indicates a registry-installed app."""
    return source.startswith(SOURCE_REGISTRY_PREFIX)


def registry_name_from_source(source: str) -> str:
    """Extract the app name from a ``registry:<name>`` source string."""
    return source[len(SOURCE_REGISTRY_PREFIX) :]


def _external_registry_repos() -> set[str]:
    """Repo names of apps in the user's configured external (federated) registries.

    Reads each registry index from the local sync cache only (``ignore_ttl`` so a
    stale index still resolves) — never fetches, so it is safe to call from the
    per-request blob-proxy worker thread. Fails open to an empty set; the caller
    treats these as additive to the bundled allowlist.
    """
    repos: set[str] = set()
    try:
        for reg in _effective_registries():
            cached = _read_external_registry_cache(
                _external_registry_cache_identity(reg), ignore_ttl=True
            )
            for entry in cached or []:
                if isinstance(entry, dict) and isinstance(entry.get("repo"), str) and entry["repo"]:
                    repos.add(_strip_git_target_userinfo(entry["repo"]))
    except Exception:  # fail open: the allowlist must never break blob serving
        logger.debug("_external_registry_repos: read failed", exc_info=True)
    return repos


def known_registry_repos() -> set[str]:
    """Repo names trusted by the ``/api/apps/blob`` SSRF gate.

    Union of the bundled registry and the user's external (federated)
    registries — external-registry apps resolve an ``/api/apps/blob`` iconUrl,
    so their repos must be allowlisted here or the App Store icon 403s.
    """
    bundled = {
        _strip_git_target_userinfo(e["repo"])
        for e in _load_registry_file()
        if isinstance(e.get("repo"), str) and e["repo"]
    }
    return bundled | _external_registry_repos()
