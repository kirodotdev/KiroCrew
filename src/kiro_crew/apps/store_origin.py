"""Which apps have a public id: the shipped built-ins and the apps installed from the store.

An app's public id is its name when that name is already public, so sending it
discloses nothing the operator chose. Two kinds of app qualify:

* a built-in the package ships, by the ``name`` in its ``app.json``
  (:func:`kiro_crew.apps.discovery.discover_builtin_apps`; an edition's own
  built-ins are not included);
* an app installed from the public store. Its install record says
  ``origin=registry``, a ``registry:<name>`` source, a non-empty ``sourceUrl``, an
  empty ``sourceRegistry`` and ``dev`` off (a dev-mode app serves edited code).
  ``install_from_registry`` writes exactly that for a row with no ``_registry``
  marker, the rows it also sends an install receipt for. A row from an
  owner-configured registry records that registry's id, so it is private.

An edition may name the apps of a registry it ships. Its
``AppsLoader.default_registries()`` row carries ``"appNamespace": "<namespace>"``, and
an app installed from that registry (the same record shape, with that registry's id
in ``sourceRegistry``) gets the public id ``<namespace>.<name>``. The namespace is read
only from the build's own row, never from ``config.json``, only for a row the build
pinned and still serves, and only when that registry's index lists the app at the
clone target its record names. An id that does not fit :data:`PUBLIC_ID_RE` stays private. App names
hold no ``.``, so a namespaced id never equals a built-in or store name. No row
declares a namespace in the public build, so nothing changes there.

The answer comes from the install record only, never from today's catalog, so it is
fixed when the app is installed: an app the store later delists keeps its id. A
reinstall from anywhere else rewrites the record, and that install is private.

shortcut: ``installed.json`` is writable from the app's own sandbox (see
:mod:`kiro_crew.apps.protected_app_state`), so an app's code can make its own record
look like a store install and have its own directory name sent. It can only name
itself, a name its code already holds. Move the origin into a ``member-records`` kind
if a count ever has to resist that.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The value rule the receiving metrics store applies to an ``Entity``.
PUBLIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: The ``default_registries()`` row key that names a registry's app namespace.
APP_NAMESPACE_KEY = "appNamespace"


class PublicAppIds(dict[str, str]):
    """:func:`public_app_ids`: app name -> version, plus :attr:`names`.

    ``names`` maps an app whose public id is not its own name to that id
    (``<namespace>.<name>``). It travels with the scan it came from, so a caller that
    labels from its own scan gets that scan's names too.
    """

    def __init__(self, ids: "dict[str, str]", names: "dict[str, str]") -> None:
        super().__init__(ids)
        self.names = names


@dataclass(frozen=True)
class _NamedRegistry:
    """A registry the build named: its app namespace and the rows its index lists."""

    namespace: str
    #: ``(app name, clone target)`` of each row in the registry's cached index.
    listed: "tuple[tuple[str, str], ...]"


def edition_names_apps() -> bool:
    """True when a ``default_registries()`` row of the edition declares ``appNamespace``.

    A cheap read of the raw rows: it says whether names can exist at all, not
    whether an app has one. False when the rows cannot be read.
    """
    from kiro_crew.platform import PlatformCompositionError, current_context, safe_context_call

    empty: list[dict[str, Any]] = []
    try:
        rows: Any = safe_context_call(
            lambda: current_context().apps_loader.default_registries(),
            fallback=empty,
            log_message="edition default_registries() unavailable",
        )
    except PlatformCompositionError:
        return False
    if not rows or isinstance(rows, (str, bytes, dict)) or not hasattr(rows, "__iter__"):
        return False
    return any(isinstance(r, dict) and r.get(APP_NAMESPACE_KEY) for r in rows)


def _registry_namespaces() -> dict[str, _NamedRegistry]:
    """Registry identity key -> :class:`_NamedRegistry`, for each registry the build named.

    A key counts only when a pinned build row names a namespace AND the registry is
    served (``_effective_registries``): a name that an operator registry also
    claims is served by neither, so an app recorded from the operator's registry
    of that name is not named. Empty when the edition declares none or its rows
    cannot be read: a failure here can only leave an app private, never name one.
    """
    from kiro_crew.apps import registry as reg_mod
    from kiro_crew.apps.registry_pipeline.caches import (
        _external_registry_cache_identity,
        _read_external_registry_cache,
    )
    from kiro_crew.apps.registry_pipeline.git_targets import _entry_git_url
    from kiro_crew.platform import PlatformCompositionError, current_context, safe_context_call

    empty: list[dict[str, Any]] = []
    try:
        rows: Any = safe_context_call(
            lambda: current_context().apps_loader.default_registries(),
            fallback=empty,
            log_message="edition default_registries() unavailable",
        )
    except PlatformCompositionError:
        # No composed context in this process: no edition names any registry.
        return {}
    if not rows or isinstance(rows, (str, bytes, dict)) or not hasattr(rows, "__iter__"):
        return {}
    served = {
        reg_mod._registry_identity_key(r.name or r.repo) for r in reg_mod._effective_registries()
    }
    pinned = {
        key: reg
        for reg in reg_mod._pinned_registries()
        if (key := reg_mod._registry_identity_key(reg.name or reg.repo)) in served
    }
    # Each row's namespace binds to the pinned row it materialised into: same identity
    # (``name or repo``) AND the same repository and branch, read the way
    # ``_pinned_registries`` reads them. A row it rejected (a plaintext repo) names
    # other coordinates, so it cannot lend its namespace to a kept row of that name.
    found: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        namespace, name, repo, branch = (
            row.get(APP_NAMESPACE_KEY),
            row.get("name"),
            row.get("repo"),
            row.get("branch"),
        )
        if not (
            isinstance(namespace, str) and namespace and isinstance(repo, str) and repo.strip()
        ):
            continue
        name = name.strip() if isinstance(name, str) else ""
        key = reg_mod._registry_identity_key(name or repo.strip())
        reg = pinned.get(key)
        coords = (repo.strip(), branch if isinstance(branch, str) and branch else "main")
        if reg is not None and (reg.repo, reg.branch) == coords:
            found.setdefault(key, []).append(namespace)
    # Two rows naming one registry: which namespace applies is ambiguous, so neither.
    declared = {key: got[0] for key, got in found.items() if len(got) == 1}
    if not declared:
        return {}
    out: dict[str, _NamedRegistry] = {}
    for key, namespace in declared.items():
        reg = pinned[key]
        cached = _read_external_registry_cache(
            _external_registry_cache_identity(reg), ignore_ttl=True
        )
        listed = tuple(
            (entry["name"], _entry_git_url(entry))
            for entry in cached or ()
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        )
        out[key] = _NamedRegistry(namespace, listed)
    return out


def _namespaced_id(
    registry_id: str, app: str, url: str, namespaces: dict[str, _NamedRegistry]
) -> str | None:
    """``<namespace>.<app>`` for an app from a registry the build named, else None.

    The registry's index must list *app* at the clone target the install recorded,
    the rule ``update_status._pinned_claim`` applies: a record left from an earlier
    registry of the same name names another repository, and stays private.
    """
    from kiro_crew.apps import registry as reg_mod
    from kiro_crew.apps.registry_pipeline.git_targets import _same_git_target

    named = namespaces.get(reg_mod._registry_identity_key(registry_id))
    if named is None:
        return None
    if not any(name == app and _same_git_target(target, url) for name, target in named.listed):
        return None
    public = f"{named.namespace}.{app}"
    return public if PUBLIC_ID_RE.fullmatch(public) else None


@functools.cache
def _shipped_builtin_ids() -> tuple[tuple[str, str], ...]:
    """``(name, version)`` from each built-in ``app.json`` in this package. Read once per process."""
    from kiro_crew.apps.discovery import discover_builtin_apps

    return tuple(
        (app["name"], str(app.get("version") or ""))
        for app in discover_builtin_apps()
        if isinstance(app.get("name"), str)
    )


def public_app_ids() -> PublicAppIds:
    """Every app on this machine with a public id, mapped to its version.

    A built-in's version is its shipped ``app.json``'s; a store install's is the one
    its install record names, as is that of an app from a registry the edition
    named, whose public id is in ``names``. Reads the install records: call off the
    loop.
    """
    from kiro_crew.apps.manager import (
        InstalledRecordShapeError,
        _is_sidecar_entry,
        _read_installed,
        apps_dir,
    )
    from kiro_crew.apps.update_status import _recorded_source

    ids = dict(_shipped_builtin_ids())
    names: dict[str, str] = {}
    # Read only when a record names a registry, so a machine with none never asks.
    namespaces: dict[str, _NamedRegistry] | None = None
    root = apps_dir()
    if not root.is_dir():
        return PublicAppIds(ids, names)
    for entry in root.iterdir():
        if _is_sidecar_entry(entry.name) or not entry.is_dir():
            continue
        try:
            meta = _read_installed(entry.name)
        except (InstalledRecordShapeError, OSError, ValueError) as exc:
            logger.debug("public app ids: skipping %s: %s", entry.name, exc)
            continue
        if meta is None or meta.origin != "registry" or meta.dev:
            continue
        # The update check's own reading of a registry record: (sourceUrl, sourceRegistry).
        recorded = _recorded_source(
            {
                "source": meta.source,
                "sourceUrl": meta.sourceUrl,
                "sourceRegistry": meta.sourceRegistry,
            }
        )
        if recorded is None:
            continue
        if recorded[1] == "":
            ids[entry.name] = meta.version
            continue
        if namespaces is None:
            try:
                namespaces = _registry_namespaces()
            except Exception:  # a bad edition row must not stop the other apps' ids
                logger.debug("public app ids: registry namespaces unreadable", exc_info=True)
                namespaces = {}
        public = _namespaced_id(recorded[1], entry.name, recorded[0], namespaces)
        if public is not None:
            ids[entry.name] = meta.version
            names[entry.name] = public
    return PublicAppIds(ids, names)


async def count_install(name: str, via: str, *, was_installed: bool) -> None:
    """Count *name*'s registry install, after its provenance is on disk.

    A first install counts ``kirocrew.app.installed``; one over an installed app
    counts ``kirocrew.app.updated``. The public ids are re-read first, off the loop,
    and the count is labelled from that read, so it names an app this install just
    made public, at the version it just wrote. Never raises past a cancellation.
    """
    try:
        from kiro_crew.metrics.feature_usage import (
            record_app_installed,
            record_app_updated,
            refresh_public_app_ids,
        )

        # Labelled from this call's own scan: a concurrent refresh can swap the
        # shared map for an older scan while this one waits.
        ids = await asyncio.to_thread(refresh_public_app_ids)
        (record_app_updated if was_installed else record_app_installed)(name, via, ids)
    except Exception:  # recording never changes the install's result
        logger.debug("install count failed for %s", name, exc_info=True)
