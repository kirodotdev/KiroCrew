"""The on-disk caches under ``cache/app-manifests``: identity, paths, reads, writes.

Two caches share the directory. Per-app ``app.json`` files live in ``by-source/``,
keyed by the row's full source coordinates and garbage-collected on write. External
registry indexes live at the root, keyed by the registry's ``name|repo|branch``
identity and stored credential-free, together with the migration off the older
name-keyed and credential-bearing filenames. Every cached index entry passes the
registry's name and subdirectory gates on every read.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from kiro_crew.apps.registry_pipeline import _FACADE, _facade
from kiro_crew.apps.registry_pipeline.git_targets import (
    _entry_git_url,
    _normalize_git_target,
    _public_registry_name,
    _strip_git_target_userinfo,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(_FACADE)


# Manifest cache: fetched app.json files from repos
def _manifest_cache_dir() -> Path:
    return config_dir() / "cache" / "app-manifests"


_MANIFEST_CACHE_TTL = 86400  # 24 hours


#: Subdirectory of :func:`_manifest_cache_dir` holding the source-keyed
#: manifest files. Registry INDEX caches live at the dir root; keeping the
#: manifest files in a subdirectory separates the two by something an external
#: index cannot spell in an app name (``_safe_cache_stem`` returns plain names
#: byte-identical, so a name-prefix convention would be imitable — an app
#: literally named ``_registry_x`` must not be able to place its manifest
#: outside the garbage collector's reach).
_MANIFEST_SOURCE_SUBDIR = "by-source"


#: How far past its TTL a cache file's mtime is pushed by
#: :func:`_expire_cache_file`. The GC grace below is derived from this so
#: expiry (which deliberately preserves the file) and reclamation (which
#: deletes it) can never collide however this slack changes.
_CACHE_EXPIRY_BACKDATE_SLACK = 3600


def _safe_cache_stem(name: str) -> str:
    """Map an arbitrary registry/app name to a filesystem-safe cache stem.

    Pure-safe names (``[A-Za-z0-9_.\\-]``, no ``..``) are returned byte-identical
    so existing caches stay valid. Any name carrying disallowed characters —
    crucially path separators or ``..`` traversal supplied by an external
    registry entry (e.g. ``../../config``) — is slugified AND disambiguated with
    a short stable hash of the ORIGINAL name, so the derived path can never
    escape ``_manifest_cache_dir()`` nor collide with another name.
    """
    if ".." not in name and re.match(r"^[A-Za-z0-9_.\-]+$", name):
        return name
    slug = re.sub(r"[^A-Za-z0-9_\-]+", "-", name).strip("-") or "app"
    digest = sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _manifest_source_coordinates(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    """The full source coordinates a cached manifest's identity is scoped to.

    Returns ``(origin, ref, subdirectory, name)`` where *origin* is the
    normalized, credential-free clone URL (:func:`_normalize_git_target`
    strips userinfo, so a token in a configured URL never reaches a cache
    file name or key material), *ref* is the effective ref — always the
    configured branch, plus the pinned commit when the row carries one — and
    *subdirectory*/*name* are the entry's remaining coordinates.

    The branch is folded in even when a commit is present: the listing fetch
    resolves non-catalog rows by BRANCH (their pins are data fidelity, not
    authorization), so a ref that kept only the commit would hold the cache
    path fixed across an operator's branch change — the exact reuse this
    identity exists to rule out. The pin is folded in as well so a
    republished pin is a miss rather than a stale hit.

    Every value an external index controls degrades to a safe default when it
    is not a string: the coordinates feed a cache KEY, so a malformed value
    must produce a distinct-but-harmless identity, never a crash.
    """
    name = entry.get("name", "")
    if not isinstance(name, str):
        name = ""
    git_url = _entry_git_url(entry)
    origin = _normalize_git_target(git_url) if git_url else ""
    commit = entry.get("commit")
    branch = entry.get("branch", "main")
    if not isinstance(branch, str) or not branch:
        branch = "main"
    ref = f"branch:{branch}"
    if isinstance(commit, str) and commit:
        ref = f"{ref}|commit:{commit}"
    subdirectory = entry.get("subdirectory", "")
    if not isinstance(subdirectory, str):
        subdirectory = ""
    return origin, ref, subdirectory, name


def _manifest_cache_path(entry: dict[str, Any]) -> Path:
    """Cache file for *entry*'s fetched ``app.json``, keyed on SOURCE IDENTITY.

    The stem sanitizes the name so a hostile/traversal entry name from an
    external registry can never resolve outside the manifest cache dir (read,
    write, AND expiry all go through here, so they stay mutually consistent).
    The digest folds the full source coordinates — normalized credential-free
    origin, effective branch/pinned commit, repository subdirectory, and app
    name — into the identity, so changing the configured branch is a cache
    MISS by construction and two same-name apps from different repositories
    can never share (or poison) each other's cached metadata. Name-keyed
    caching could not establish provenance: a listing configured for branch
    ``dev`` happily reused a manifest resolved earlier from ``main``.
    """
    origin, ref, subdirectory, name = _manifest_source_coordinates(entry)
    # json.dumps gives each coordinate an escaped, delimited slot, so a value
    # containing a would-be separator can never make two different coordinate
    # tuples serialize to the same key material.
    material = json.dumps([origin, ref, subdirectory, name])
    digest = sha256(material.encode("utf-8")).hexdigest()[:16]
    return (
        _manifest_cache_dir() / _MANIFEST_SOURCE_SUBDIR / f"{_safe_cache_stem(name)}-{digest}.json"
    )


def _read_manifest_cache(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Read cached app.json for a registry entry's exact source coordinates.

    Returns None if missing or stale — and, because the path is derived from
    the entry's full source identity, also None whenever the branch, pinned
    commit, repository, or subdirectory differ from what was cached, so a
    failed fetch can never silently fall back to another source's manifest.
    """
    path = _manifest_cache_path(entry)
    if not path.is_file():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > _MANIFEST_CACHE_TTL:
            return None  # stale
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


#: Extra age beyond the largest TTL before a cache file is reclaimed. Derived
#: from the expiry backdate slack so a file :func:`_expire_cache_file` just
#: backdated (whose whole point is surviving its expiry) is never GC-eligible
#: in the same breath, whatever value the slack takes.
_MANIFEST_CACHE_GC_GRACE = _CACHE_EXPIRY_BACKDATE_SLACK + 2 * 86400


def _gc_manifest_cache_dir() -> None:
    """Best-effort reclamation of manifest cache files nothing can read anymore.

    Coordinate-keyed cache files are orphaned whenever a row's branch, pin,
    repository, or subdirectory changes: the new coordinates write a NEW file
    and no reader ever derives the old path again. An untrusted index that
    churns its coordinates every refresh would otherwise grow the cache dir
    without bound. Reclaim is age-based and read-invisible: only files older
    than every TTL plus a grace window are removed, and ``_read_manifest_cache``
    already answers ``None`` for anything past ``_MANIFEST_CACHE_TTL`` (there
    is no ``ignore_ttl`` read of a manifest file), so deleting them changes no
    read result. Only the ``by-source/`` subdirectory is scanned: registry
    index caches live at the cache-dir ROOT, so the boundary between "GC may
    reclaim" and "GC never touches" is structural — an index cannot spell a
    directory into an app name, where a name-prefix convention (skip
    ``_registry_*``) would be imitable and hand a hostile index files the
    sweep never reclaims. Runs on the write path because writes are the only
    way the directory grows, which bounds it by construction.
    """
    cutoff = (
        time.time()
        - max(_MANIFEST_CACHE_TTL, _EXTERNAL_REGISTRY_CACHE_TTL)
        - _MANIFEST_CACHE_GC_GRACE
    )
    try:
        entries = list((_manifest_cache_dir() / _MANIFEST_SOURCE_SUBDIR).iterdir())
    except OSError:
        return
    for path in entries:
        if not path.name.endswith(".json"):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _write_manifest_cache(entry: dict[str, Any], data: dict[str, Any]) -> None:
    """Write app.json to the manifest cache (atomic), keyed on source identity."""
    path = _manifest_cache_path(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(
            path,
            json.dumps(data, indent=2) + "\n",
        )
    except OSError as exc:
        logger.warning("Failed to cache manifest for %s: %s", entry.get("name", ""), exc)
    _gc_manifest_cache_dir()


_EXTERNAL_REGISTRY_CACHE_TTL = 3600  # 1 hour


def _credential_free_external_registry_value(value: Any) -> Any:
    """Recursively sanitize URI-shaped strings before a row is retained."""
    if isinstance(value, str):
        candidate = value.strip()
        if "://" in candidate:
            return _strip_git_target_userinfo(candidate)
        return value
    if isinstance(value, list):
        return [_credential_free_external_registry_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _credential_free_external_registry_value(item) for key, item in value.items()}
    return value


def _credential_free_external_registry_entries(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [_credential_free_external_registry_value(entry) for entry in entries]


def _external_registry_cache_identity(reg: Any) -> str:
    """Stable cache identity for one configured registry source.

    A display name is not provenance: operators may repoint the same name to a
    different repository or branch. Include the normalized credential-free
    source coordinates so stale-fallback readers cannot answer from the old
    source after that change.

    ``branch`` is read defensively: registry objects reaching this helper are
    duck-typed and may not carry the attribute at all. An absent branch, a
    ``None`` branch, and an empty-string branch all mean "the source's default
    branch" and share one identity component (the empty string), which can
    never collide with a real branch because a configured branch is always a
    non-empty string.
    """
    name = _public_registry_name(reg)
    repo = _normalize_git_target(reg.repo)
    branch = str(getattr(reg, "branch", "") or "")
    return f"{name}|{repo}|{branch}"


def _external_registry_cache_path_for_identity(name: str, *, slug_cap: int | None = None) -> Path:

    # Pure-safe names map to the historical byte-identical path (no hash
    # suffix). A coordinate identity from _external_registry_cache_identity
    # always contains "|", so live index caches always take the slug+digest
    # form; the byte-identical branch remains load-bearing for LEGACY path
    # computation (_legacy_external_registry_cache_path and the name-keyed
    # cleanup need to derive exactly the file an older release wrote). Names
    # carrying disallowed characters are slugified AND disambiguated with a
    # stable hash of the ORIGINAL name.
    #
    # ``slug_cap`` bounds the human-readable prefix for CURRENT identity
    # paths (an URL-derived name repeats much of the repo URL, and an
    # over-long filename makes every cache write fail with ENAMETOOLONG,
    # silently disabling the stale-fallback). A capped prefix uses the FULL
    # SHA-256 digest so truncation cannot reduce collision resistance. The
    # DEFAULT is uncapped and keeps the historical eight-hex digest: that is
    # the byte-identical derivation every previous release used, and legacy
    # cleanup must keep deriving exactly those paths — changing its digest or
    # capping its slug would miss an existing legacy artifact.
    if re.match(r"^[A-Za-z0-9_\-]+$", name):
        safe = name
    else:
        slug = re.sub(r"[^A-Za-z0-9_\-]+", "-", name).strip("-") or "registry"
        full_digest = sha256(name.encode("utf-8")).hexdigest()
        if slug_cap is not None:
            slug = slug[:slug_cap].strip("-") or "registry"
            digest = full_digest
        else:
            digest = full_digest[:8]
        safe = f"{slug}-{digest}"
    return _manifest_cache_dir() / f"_registry_{safe}.json"


def _external_registry_cache_path(name: str) -> Path:
    safe_name = _credential_free_external_registry_value(name)
    return _external_registry_cache_path_for_identity(safe_name, slug_cap=120)


def _legacy_external_registry_cache_path(name: str) -> Path:
    """The pre-hardening path, used only to remove an exact legacy artifact."""
    return _external_registry_cache_path_for_identity(name)


def _remove_legacy_credential_registry_cache(name: str) -> None:
    """Best-effort removal of a cache whose old filename exposed URL userinfo."""
    legacy_path = _legacy_external_registry_cache_path(name)
    safe_path = _external_registry_cache_path(name)
    if legacy_path == safe_path:
        return
    try:
        legacy_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to remove a legacy credential-bearing registry cache")


def _remove_legacy_name_keyed_registry_cache(reg: Any) -> None:
    """Best-effort removal of caches written under the pre-identity key.

    Before the cache identity included source coordinates, the index cache
    was keyed on ``reg.name or reg.repo`` alone. No reader derives that path
    any more, so the file is reclaimed here rather than left behind — in BOTH
    filename forms: the sanitized one a recent release wrote, and the raw one
    an older release wrote, whose filename can embed URL userinfo when the
    display name is a credential-bearing URL. Runs before the fetch so the
    credential-bearing artifact is removed even when the registry is
    unreachable. Both derivations are deliberately UNCAPPED
    (``slug_cap=None``): previous releases wrote uncapped slugs, and a capped
    derivation would miss any legacy file whose slug ran past the cap.
    """
    legacy_name = str(getattr(reg, "name", "") or "") or reg.repo
    current_path = _external_registry_cache_path(_external_registry_cache_identity(reg))
    for legacy_path in (
        _external_registry_cache_path_for_identity(
            _credential_free_external_registry_value(legacy_name)
        ),
        _legacy_external_registry_cache_path(legacy_name),
    ):
        if legacy_path == current_path:
            continue
        try:
            legacy_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove a legacy name-keyed registry cache")


def _read_external_registry_cache(
    name: str,
    *,
    ignore_ttl: bool = False,
) -> list[dict[str, Any]] | None:
    """Read cached external registry entries. Returns None if missing or stale.

    When *ignore_ttl* is True, returns data regardless of age — used by
    synchronous callers that cannot refresh the cache themselves.
    """
    _remove_legacy_credential_registry_cache(name)
    path = _external_registry_cache_path(name)
    if not path.is_file():
        return None
    try:
        original_stat = path.stat()
        is_stale = time.time() - original_stat.st_mtime > _EXTERNAL_REGISTRY_CACHE_TTL
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return None
        sanitized_data = _credential_free_external_registry_value(data)
        if sanitized_data != data:
            # Named registries keep the same cache path across this migration.
            # Rewrite their legacy payload in place so the secret is not merely
            # hidden from this read while remaining durable on disk. Preserve
            # the original timestamps so cleaning an expired cache cannot make
            # it appear fresh and bypass the TTL-driven network refresh.
            try:
                atomic_write(path, json.dumps(sanitized_data, indent=2) + "\n")
                os.utime(
                    path,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
            except OSError:
                # If migration cannot be completed, remove the old artifact
                # rather than leave credential-bearing JSON durable on disk.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove a credential-bearing legacy registry cache")
        data = sanitized_data
        if is_stale and not ignore_ttl:
            return None
        # Path-safety gate on EVERY cache read, stale-fallback reads included: the
        # name and subdirectory gates live in the facade (see
        # ``registry._admit_cached_index_entries``), reached at call time.
        return _facade()._admit_cached_index_entries(data)
    except (json.JSONDecodeError, OSError):
        return None


def _write_external_registry_cache(name: str, entries: list[dict[str, Any]]) -> None:
    """Write external registry entries to cache."""
    _remove_legacy_credential_registry_cache(name)
    _manifest_cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(
            _external_registry_cache_path(name),
            json.dumps(_credential_free_external_registry_entries(entries), indent=2) + "\n",
        )
    except OSError:
        logger.warning("Failed to cache external registry")


def _expire_cache_file(path: Path) -> None:
    """Backdate a cache file's mtime so it reads as stale (best-effort).

    Preferred over unlinking: a subsequent read treats the file as expired and
    refetches, but the data survives on disk as a stale-fallback if that
    refetch fails — so a refresh during a forge/network blip degrades to
    "slightly stale" instead of "apps vanished". Missing file is a no-op.

    Defense-in-depth: the resolved path must stay inside the manifest cache
    dir; anything else (a traversal-derived path) is ignored rather than
    touched. In practice ``_manifest_cache_path`` already sanitizes names, so
    this only guards against future callers.
    """
    try:
        cache_dir = _manifest_cache_dir().resolve()
        resolved = path.resolve()
        if cache_dir not in resolved.parents:
            logger.warning("Refusing to expire cache file outside cache dir: %s", path)
            return
        past = (
            time.time()
            - max(_MANIFEST_CACHE_TTL, _EXTERNAL_REGISTRY_CACHE_TTL)
            - _CACHE_EXPIRY_BACKDATE_SLACK
        )
        os.utime(resolved, (past, past))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Failed to expire cache file %s: %s", path, exc)
