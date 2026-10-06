"""Store art for a not-installed app: the manifest paths that name it, and the
owner-tier prewarm that puts its bytes where the blob proxy serves from.

The App Store shows an app's icon, hero and screenshots BEFORE it is installed.
For an app in a public repository the blob proxy fetches those on demand with a
credential-free clone (see ``anonymous_git_env``). An ``owner``-tier registry is
the case that clone cannot serve: its apps live in private repositories on the
operator's own forge, so every automatic browse-time fetch fails and the whole
catalog renders as generic cubes with no screenshots. Installing works (the
install path re-confirms the row against a fresh index and clones with owner
credentials), so the same app is bare in the store and fully dressed once
installed -- a difference in what the operator can SEE, not in what they trust.

:func:`_prewarm_owner_tier_store_assets` closes that gap at the one moment the
trust question already has its answer: right after the registry's index was
fetched FRESH. The rows it receives never touched the agent-writable index cache
-- they are the parsed bytes of the index the build pinned, still in memory --
which is precisely the authority ``_owner_tier_confirmed`` re-fetches that same
index to obtain before an install. Each row's repository is shallow-cloned once
with owner credentials, its ``app.json`` is written to the manifest cache and the
image files that manifest declares are written to the blob cache under the exact
key and path the blob proxy computes for that row. Browsing then hits both
caches and clones nothing; a miss keeps the credential-free posture unchanged.

What the prewarm does NOT do, each of which is why the trust argument holds:

- It never trusts a cached row. Both callers hand it the list a fresh fetch
  returned, in the same call, before anything read the cache back.
- It never runs anything from the clone. It reads ``app.json`` and copies image
  files; ``setup.onInstall`` stays where it was.
- It never widens the host set: ``is_clone_host_trusted`` gates every clone.
- It never escalates a registry that is not ``owner``: the tier is read with
  ``_registry_trust_tier_of`` off the row whose index was fetched, which is
  ``owner`` only for a build-pinned row or an operator-granted repository.
- It never writes a file the proxy would refuse to serve: the same extension
  allowlist, path grammar and traversal rules are applied to each declared path
  BEFORE it is read, and the bytes are read from the resolved clone tree with a
  containment check, so a hostile ``app.json`` cannot name a file outside it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
import stat
import tempfile
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline.caches import (
    _EXTERNAL_REGISTRY_CACHE_TTL,
    _MANIFEST_CACHE_TTL,
    _blob_cache_dir,
    _manifest_cache_path,
    _read_manifest_cache,
    _write_manifest_cache,
)
from kiro_crew.apps.registry_pipeline.checkout import (
    _KILL_GRACE_PERIOD,
    _git_fetch_branch,
    _rmtree_force_settled,
)
from kiro_crew.apps.registry_pipeline.git_targets import (
    _entry_git_url,
    _git_target_is_unsupported,
    _looks_like_git_url,
    _public_registry_name,
    _redacted_git_failure_class,
    _same_git_target,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.sources import (
    _TRUST_OWNER,
    _context_clone_sandbox_mode,
    _is_supported_registry_transport,
    _registry_trust_tier_of,
    _repo_key_claims,
    _sel_credential_decision,
    _sel_credential_grant,
    is_clone_host_trusted,
)
from kiro_crew.apps.registry_pipeline.subprocess_env import minimal_env
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    is_reparse_point,
    open_pinned_descendant_dir,
)

logger = logging.getLogger(_FACADE)


# ---------------------------------------------------------------------------
# Path grammar shared with the manifest merge and the blob proxy
# ---------------------------------------------------------------------------


def _is_safe_registry_subdir(subdir: Any) -> bool:
    """True if *subdir* is a safe, contained relative path for a registry entry.

    An external registry index is untrusted and controls the entire entry,
    including ``subdirectory`` — which is later joined to the throwaway clone
    dir, the persistent app-source dir, and the manifest read path. An absolute
    or ``..`` value would escape those roots and let an attacker-selected
    ``app.json`` (→ ``setup.onInstall``) be read/executed. Empty/missing means
    the repo root (safe). Rejects non-strings, NUL, backslashes (Windows/UNC
    separators), absolute paths (POSIX ``/…`` or drive-letter ``C:…``), and any
    ``.``/``..`` path segment. Purely lexical; the use-site
    :func:`_contained_join` adds a symlink-resolving containment check as
    defense-in-depth.
    """
    if subdir in (None, ""):
        return True
    if not isinstance(subdir, str):
        return False
    if "\x00" in subdir or "\\" in subdir:
        return False
    if subdir.startswith("/") or (len(subdir) >= 2 and subdir[1] == ":"):
        return False
    # A repository path, so its separator is POSIX ``/`` on every OS -- the split
    # is on the git path grammar, not on the host's path separator. Split, not
    # ``PurePosixPath.parts``: that would collapse the ``.`` segment this refuses.
    return not any(seg in ("..", ".") for seg in subdir.split(posixpath.sep))


def _contained_join(root: Path, subdir: str) -> Path | None:
    """Join *subdir* under *root*, returning the symlink-resolved result only if
    it stays within *root*; ``None`` on any escape.

    Defense-in-depth companion to :func:`_is_safe_registry_subdir`: the lexical
    gate rejects ``..``/absolute values before an entry is cached/listed, and
    this resolves symlinks so a hostile clone containing e.g. ``sub -> /etc``
    cannot smuggle a read outside the clone root at use time. Returns *root*
    unchanged for an empty *subdir*.
    """
    if not subdir:
        return root
    try:
        base = root.resolve()
        target = (root / subdir).resolve()
    except (OSError, RuntimeError):
        # What non-strict ``Path.resolve`` raises: ``OSError`` for a path it
        # cannot walk, ``RuntimeError`` for a symlink loop (POSIX re-raises ELOOP
        # as one). A loop is an escape that resolves nowhere, so it fails closed
        # like every other escape -- the callers that re-check containment after
        # a third-party script wrote to the checkout depend on this returning
        # rather than raising.
        return None
    if target.is_relative_to(base):
        # ``target`` is textually contained, but on Windows a self-pointing
        # reparse point (``pkg -> pkg``) is collapsed LEXICALLY by non-strict
        # ``resolve`` -- it never walks the link, so a loop slips through here as
        # a contained-looking path that a caller would then read/write THROUGH.
        # POSIX already raised above; Windows does not, so re-resolve strictly to
        # force the OS to walk the target. The distinction that matters:
        #   - ``FileNotFoundError`` -- the path simply does not exist. That is a
        #     legitimate state some callers rely on (the rollback path re-checks
        #     containment of ``app.json`` after it has been removed, and needs a
        #     contained path back so the restore proceeds), so preserve the
        #     pre-existing contract of returning the contained path; every caller
        #     does its own existence check downstream.
        #   - any OTHER resolution error -- a loop, a component that is not a
        #     directory, a permission wall -- is a path that does not truly
        #     resolve, so fail closed. A self-pointing loop is exactly this case:
        #     the link exists, so it is not FileNotFoundError, and walking it
        #     raises on both platforms.
        try:
            target.resolve(strict=True)
        except FileNotFoundError:
            return target
        except (OSError, RuntimeError):
            return None
        return target
    return None


def _store_asset_path(subdirectory: Any, asset_path: Any) -> Any:
    """Repo-root-relative path of a store-card asset declared in ``app.json``.

    The manifest is read from ``_contained_join(clone_dir, subdirectory)``, so
    every art path it declares (``iconPath``, ``heroImage*``, ``screenshots*``)
    is relative to that directory -- while ``/api/apps/blob`` resolves ``path``
    against the repo root. This is the store-card reader's join; the field
    itself keeps its meaning, because the installed-app reader
    (``handle_app_art_file``) resolves the same value against the install
    directory, where the subdirectory has already been stripped by the install.

    Containment is preserved rather than re-derived: a ``subdirectory`` the
    lexical gate :func:`_is_safe_registry_subdir` rejects (absolute, ``..``,
    backslash) is NOT joined, so the join never manufactures a traversing path
    -- such entries are dropped before listing anyway, and the bare path here
    is exactly what the store built before. Empty or ``.`` means the repo root
    (unchanged), an absolute path or URL is left untouched, and the join is a
    plain posix join with no normalisation, so a ``..`` inside the asset path
    still reaches the blob route's own rejection unchanged.
    """
    if not asset_path or not isinstance(asset_path, str) or not isinstance(subdirectory, str):
        return asset_path
    subdir = subdirectory.rstrip("/")
    if subdir in ("", "."):
        return asset_path
    if not _is_safe_registry_subdir(subdir):
        return asset_path
    if asset_path.startswith("/") or "://" in asset_path:
        return asset_path
    return posixpath.join(subdir, asset_path)


#: Store-art fields a manifest may declare as ONE path. The blob proxy's
#: ``_merge_manifest`` rewrites exactly these into ``/api/apps/blob`` URLs and the
#: installed-app art route serves exactly these, so the prewarm copies exactly
#: these: a field missing here is a field the store never asks for.
_ART_MANIFEST_FIELDS = (
    "iconPath",
    "iconPathDark",
    "heroImage",
    "heroImageDark",
    "heroImageDetail",
    "heroImageDetailDark",
)

#: The same, for the fields that hold a LIST of paths.
_ART_MANIFEST_LIST_FIELDS = ("screenshots", "screenshotsDark")

#: Ceiling on how many paths one LIST art field contributes per row. An owner-tier
#: index is external input and a row's ``screenshots``/``screenshotsDark`` can list an
#: unbounded number of images; each declared path the prewarm accepts is one clone
#: read and one blob-cache file up to :data:`_ART_MAX_BYTES`, so without a cap one row
#: could fill the cache and one clone could copy an arbitrary number of files. Twelve
#: is generous against any real store card (the App Store shows a handful) while
#: bounding the per-row footprint; paths past it are dropped in field order, so the
#: cap is deterministic and the same paths are dropped from the copy AND from the warm
#: check (both go through :func:`_declared_store_art`), leaving no dropped path able
#: to keep the row cold. The single-path fields (icon/hero) are not list-shaped and
#: are not capped.
_MAX_LIST_ART_PATHS_PER_FIELD = 12

#: Images only. Store art is rendered into an ``<img>``, so nothing script-shaped
#: (``.mjs``/``.js``) or data-shaped (``.json``) belongs here. ``.svg`` stays because
#: an SVG loaded as an ``<img>`` source cannot execute script.
#:
#: ONE set for all three art readers -- the installed-app route, the blob proxy and
#: this prewarm. The parity is load-bearing rather than incidental: a file one of
#: them would serve and another refuses means the same app's art renders or 403s
#: depending only on whether it happens to be installed, or on whether its bytes
#: arrived through the prewarm or through an on-demand fetch.
_ART_IMAGE_EXTENSIONS = frozenset({".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"})

#: Ceiling on one art file. Generous against the publishing guide's own limits -- a
#: 512px icon, a 16:9 hero -- so a real asset never meets it, while an ``app.json``
#: cannot make the gateway copy an arbitrarily large file by declaring one.
_ART_MAX_BYTES = 8 * 1024 * 1024

#: Preamble of a Git LFS POINTER file. When a repository tracks an image through Git
#: LFS, a plain clone (which the prewarm does, without ``git lfs``) checks out a tiny
#: text POINTER in the image's place, not the image: it begins with this line, e.g.
#: ``version https://git-lfs.github.com/spec/v1``. Publishing that pointer text under
#: the image's name would serve a few lines of ASCII as an ``<img>`` source -- a
#: broken card, never the picture. The publishing guide already states LFS is
#: unsupported; this is the code-side detection, so a declared LFS-tracked asset is
#: treated as unobtainable (recorded, never published) exactly like an absent one,
#: rather than caching a pointer. Matched as bytes so a non-UTF-8 art file cannot even
#: reach a decode. A real image never starts with these bytes.
_LFS_POINTER_PREAMBLE = b"version https://git-lfs.github.com/spec/"

#: Ceiling on the ``app.json`` this prewarm reads into memory. The manifest is read
#: whole (``read_text``) and the prewarm runs several rows concurrently, so an
#: owner-tier repo shipping a huge ``app.json`` could exhaust gateway memory; the row
#: is refused before the read when the file exceeds this. A real manifest is a few
#: kilobytes, so this ceiling never meets a legitimate one.
_MANIFEST_MAX_BYTES = 1024 * 1024

#: The blob proxy's ``path`` grammar -- ONE spelling, owned here: ``routes`` imports
#: it for the request side, and the prewarm applies it to a declared path BEFORE it
#: is read so it never writes a cache file the proxy would refuse a request for.
#: ``\Z``, not ``$``: Python's ``$`` also matches immediately BEFORE a trailing
#: newline, so ``"icon.png\n"`` would pass a ``$``-anchored check -- and the value
#: feeds a filesystem join.
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+\Z")

#: The branch grammar -- ONE spelling, owned here: the index fetch (``indexes``)
#: applies it to the configured branch and the prewarm to a cross-repo row's
#: index-declared branch, both before the value reaches a clone argv.
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-./]*$")


def _blob_cache_key(repo: str, clone_url: str = "") -> str:
    """Derive a flat, filesystem-safe AND injective cache key for a repo.

    ``repo`` may be a full git URL (``/``, ``:``), so it can't be used as a
    directory tree.  Slugification alone is not injective (``org/app`` and
    ``org_app`` would collide and serve each other's blobs), so a short stable
    sha256 is appended to guarantee distinct repos never share a cache directory.

    The cache key is bound to the blob's PROVENANCE — the resolved clone URL
    (``clone_url``), not the ``repo`` key alone.  A ``repo`` key is not stable
    provenance: two registries can publish the same ``repo`` key over time
    (registry A is removed and registry B is later configured reusing key X), so
    a key derived from ``repo`` alone would let B's request hit A's cached
    (possibly private) bytes — a stale-provenance cross-registry read.  Folding
    the resolved clone URL into the hash namespaces the cache by the URL the
    bytes were actually cloned from, so a repo-key reuse across registries lands
    in a DISTINCT cache directory (a miss, then a fresh clone of B's own URL)
    rather than serving A's stale bytes.  ``clone_url`` defaults to empty only so
    the pure key of a bare-name repo with no resolvable URL stays stable; when a
    URL is resolved it MUST be threaded in.
    """
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", repo)
    digest = sha256(f"{repo}\x00{clone_url}".encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


def _declared_store_art(manifest: dict[str, Any]) -> list[str]:
    """The art paths *manifest* declares, in field order, duplicates dropped.

    Values are returned as declared: the merge rewrites the declared string into
    the blob URL verbatim, so the cache path must be built from that same string
    and not from a normalised form of it.

    Each LIST field (``screenshots``/``screenshotsDark``) contributes at most
    :data:`_MAX_LIST_ART_PATHS_PER_FIELD` paths, in declared order: an owner-tier
    index is external and can list an unbounded number of screenshots, and every
    accepted path is a clone read and a blob-cache file. The cap is applied to the
    raw list slice, so it bounds the row deterministically whether it is reached from
    the copy or from the warm check (both call this), and a dropped path can never
    keep the row cold.
    """
    declared: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            declared.append(value)

    for key in _ART_MANIFEST_FIELDS:
        _add(manifest.get(key))
    for key in _ART_MANIFEST_LIST_FIELDS:
        values = manifest.get(key)
        if isinstance(values, list):
            for value in values[:_MAX_LIST_ART_PATHS_PER_FIELD]:
                _add(value)
    return declared


def _dropped_list_art_paths(manifest: dict[str, Any]) -> dict[str, int]:
    """How many paths each LIST art field declares past the per-field cap.

    The counterpart of the slice in :func:`_declared_store_art`: the merge still
    emits a URL for every declared screenshot, so a path past the cap stays cold and
    the overflow must be counted and said out loud. Only fields that overflow are
    returned; an empty dict means nothing was dropped.
    """
    dropped: dict[str, int] = {}
    for key in _ART_MANIFEST_LIST_FIELDS:
        values = manifest.get(key)
        if isinstance(values, list) and len(values) > _MAX_LIST_ART_PATHS_PER_FIELD:
            dropped[key] = len(values) - _MAX_LIST_ART_PATHS_PER_FIELD
    return dropped


def _servable_art_path(asset_path: str) -> bool:
    """Whether the blob proxy would accept *asset_path* as its ``path`` query.

    Mirrors the route's request gates in order: grammar, traversal, hidden
    segments (``.git/...``), image extension. Refusing here means the prewarm never
    reads -- let alone caches -- a file the proxy would 400 or 403 a request for.
    """
    if not _SAFE_PATH_RE.match(asset_path):
        return False
    if ".." in asset_path or asset_path.startswith("/"):
        return False
    if any(seg.startswith(".") for seg in Path(asset_path).parts):
        return False
    return Path(asset_path).suffix.lower() in _ART_IMAGE_EXTENSIONS


def _store_art_cache_path(entry: dict[str, Any], asset_path: str) -> Path | None:
    """Where the blob proxy will look for *asset_path* of *entry*, or None.

    Reproduces the proxy's resolution for the URL ``_merge_manifest`` emits for
    this row: ``repo`` is the credential-free row repo, ``clone_url`` the row's
    effective git URL, ``ref`` the row's branch (the proxy falls back to it when
    the URL carries no ``ref``), and ``path`` the asset joined under the row's
    ``subdirectory``. Any coordinate the proxy would refuse yields None.
    """
    raw_repo = entry.get("repo", "")
    repo = _strip_git_target_userinfo(raw_repo) if isinstance(raw_repo, str) else ""
    clone_url = _entry_git_url(entry)
    branch = entry.get("branch", "main")
    if not repo or not clone_url or not isinstance(branch, str) or not branch:
        return None
    if not _SAFE_BRANCH_RE.match(branch) or ".." in branch:
        return None
    file_path = _store_asset_path(entry.get("subdirectory", ""), asset_path)
    if not isinstance(file_path, str) or not _servable_art_path(file_path):
        return None
    cache_root = _blob_cache_dir()
    cache_path = cache_root / _blob_cache_key(repo, clone_url) / branch / file_path
    # The proxy resolves its cache path against the cache root before touching it;
    # do the same so a coordinate that slipped the lexical gates still cannot name
    # a file outside the blob cache.
    try:
        resolved_root = cache_root.resolve()
        resolved = cache_path.resolve()
    except (OSError, RuntimeError):
        # ``RuntimeError`` is what non-strict ``Path.resolve`` raises for a
        # symlink loop under the cache root; a loop is an escape, so it fails
        # closed like every other one instead of surfacing as a 500.
        return None
    if not resolved.is_relative_to(resolved_root):
        return None
    return cache_path


#: Age past which a warm manifest is refreshed anyway. The index is re-fetched
#: every ``_EXTERNAL_REGISTRY_CACHE_TTL`` and the prewarm runs only on that fetch,
#: so a manifest that would expire BEFORE the next index fetch is renewed on this
#: one -- otherwise the store would show the row bare for up to an index interval
#: between the manifest's expiry and the next fetch.


def _check_rewarm_age(manifest_ttl: float, index_ttl: float) -> float:
    """Return the rewarm window ``manifest_ttl - index_ttl``, refusing a non-positive one.

    The rewarm window is only meaningful while the manifest TTL is wider than the
    index TTL. If the index TTL is ever raised to or past the manifest TTL, their
    difference goes <= 0, :func:`_store_assets_warm` treats every cached manifest as
    stale, and each fresh index fetch turns into a full credentialed re-clone of
    every row. Fail loudly rather than silently degrade into that.

    :data:`_REWARM_AGE` is the value this returns, NOT ``manifest_ttl - index_ttl``
    computed separately: deriving the module constant from this call is what makes
    the guard load-bearing. If the call were dropped, the constant would be
    undefined and the module would fail to import, so the check cannot be removed
    without a test going red -- which a separate ``_REWARM_AGE = a - b`` assignment
    with an advisory call beside it did not guarantee.
    """
    window = manifest_ttl - index_ttl
    if window <= 0:
        raise RuntimeError(
            "_REWARM_AGE must be positive: _MANIFEST_CACHE_TTL "
            f"({manifest_ttl}) must exceed _EXTERNAL_REGISTRY_CACHE_TTL "
            f"({index_ttl})"
        )
    return window


_REWARM_AGE = _check_rewarm_age(_MANIFEST_CACHE_TTL, _EXTERNAL_REGISTRY_CACHE_TTL)


#: Suffix of the record written beside a row's manifest cache file, naming the
#: servable-shaped art paths that manifest declares whose bytes the clone could not
#: supply (absent from the checkout, not a regular file, over :data:`_ART_MAX_BYTES`,
#: or refused by the containment check). Without it such a path has no blob-cache
#: file and never will, so the row would read as cold on every fresh index fetch and
#: be re-cloned with owner credentials each time, occupying the batch budget ahead of
#: rows that would warm. With it the path is treated as SATISFIED for as long as the
#: manifest cache it was recorded against stays fresh: one clone per manifest
#: lifetime instead of one per index fetch.
#:
#: Read ONLY by :func:`_store_assets_warm`. The blob proxy never opens this file and
#: nothing in it is ever served: a listed path is one for which NO cache file exists,
#: so a request for it is the same miss it would be without the record. The file
#: lives in the ``by-source/`` manifest cache directory and ends in ``.json``, which
#: is exactly what ``_gc_manifest_cache_dir`` sweeps, so it is reclaimed on the same
#: schedule as the manifest it annotates. Its name cannot collide with a manifest
#: cache file: those end in ``-<16 hex digits>.json`` and this suffix does not.
_UNOBTAINABLE_ART_SUFFIX = ".unobtainable.json"


def _unobtainable_art_path(entry: dict[str, Any]) -> Path:
    """The record file beside *entry*'s manifest cache file (see the suffix above)."""
    manifest_path = _manifest_cache_path(entry)
    return manifest_path.with_name(manifest_path.stem + _UNOBTAINABLE_ART_SUFFIX)


def _read_unobtainable_art(entry: dict[str, Any], manifest_mtime: float) -> frozenset[str]:
    """The declared paths recorded as unobtainable against the CURRENT manifest cache.

    Only a record at least as new as the manifest cache file counts: the record is
    written after the manifest on every prewarm, so an older one describes a
    manifest that has since been replaced and is ignored. The file sits in an
    agent-writable cache directory, so it is read ONCE through a no-follow descriptor
    (:func:`_read_pinned_sidecar`) whose opened inode is validated and whose bytes are
    bounded by ``_MANIFEST_MAX_BYTES`` -- closing the size-check-then-read race a
    by-name ``stat``/``read_text`` had -- and the freshness mtime is that same
    descriptor's. Anything unreadable, oversize, stale or malformed is the empty set --
    the answer that keeps the row cold and lets the next prewarm rewrite the record.
    """
    path = _unobtainable_art_path(entry)
    result = _read_pinned_sidecar(path, max_bytes=_MANIFEST_MAX_BYTES)
    if result is None:
        return frozenset()
    data, mtime = result
    if mtime < manifest_mtime:
        return frozenset()
    try:
        recorded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return frozenset()
    if not isinstance(recorded, list):
        return frozenset()
    return frozenset(item for item in recorded if isinstance(item, str))


def _write_unobtainable_art(entry: dict[str, Any], unobtainable: frozenset[str]) -> None:
    """Record *unobtainable* beside *entry*'s manifest cache file; remove the record
    when there is nothing to record, so a stale one from an earlier manifest cannot
    outlive the paths it named. Best-effort: a failed write leaves the row cold."""
    path = _unobtainable_art_path(entry)
    try:
        if not unobtainable:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(sorted(unobtainable)) + "\n")
    except OSError as exc:
        logger.debug(
            "store art of %s: unobtainable-path record not written: %s",
            entry.get("name", ""),
            exc,
        )


#: Backoff after a CLONE-LEVEL failure before the owner-credentialed clone is tried
#: again. A missing declared asset is handled by the ``.unobtainable`` record above,
#: but that needs a manifest, and a manifest needs a clone that reached the repo. A
#: clone that fails outright -- deleted repo, revoked credential, a forge that hangs
#: to the batch budget -- writes no manifest and no unobtainable record, so without
#: this the row is cold on every fresh index fetch and the store listing that misses
#: the index cache pays the whole clone attempt (up to :data:`_PREWARM_BATCH_BUDGET`
#: plus cancellation cleanup) once an index interval, indefinitely, with no backoff.
#:
#: Six hours: several index TTLs (:data:`_EXTERNAL_REGISTRY_CACHE_TTL`, one hour) of
#: quiet, so a genuinely dead row stops stalling the hourly listing fetch, yet far
#: below the day-scale :data:`_REWARM_AGE` so a transient outage that clears is
#: retried the same day rather than pinned dead for a manifest lifetime. Unlike the
#: unobtainable record (valid only against the manifest it annotates), this is a
#: wall-clock TTL because there is no manifest to bind it to.
_CLONE_FAILURE_BACKOFF = 6 * 60 * 60


#: Suffix of the record written beside a row's manifest cache file when the
#: owner-credentialed clone FAILED before any manifest could be read (see
#: :data:`_CLONE_FAILURE_BACKOFF`). Holds ``{"at": <epoch seconds>, "reason": <short
#: redacted class>}``. Read ONLY by the prewarm's row pre-filter; nothing in it is
#: ever served. It lives in the ``by-source/`` manifest cache directory and ends in
#: ``.json``, exactly what ``_gc_manifest_cache_dir`` sweeps, so it is reclaimed on
#: the manifest cache's schedule. Its name cannot collide with a manifest cache file
#: (those end in ``-<16 hex digits>.json``) nor with the ``.unobtainable.json`` record.
_CLONE_FAILURE_SUFFIX = ".clone-failed.json"


def _clone_failure_path(entry: dict[str, Any]) -> Path:
    """The clone-failure record file beside *entry*'s manifest cache file."""
    manifest_path = _manifest_cache_path(entry)
    return manifest_path.with_name(manifest_path.stem + _CLONE_FAILURE_SUFFIX)


def _clone_failure_reason(err: Any) -> str:
    """A credential-free class label for a clone failure, for the record's ``reason``.

    Never a slice of raw git output: an owner-credentialed clone's error text can echo
    the expanded credential-bearing URL. The ``err`` dict's ``error`` string is already
    a fixed classification (``_git_fetch_branch`` runs its transport output through
    :func:`_loggable_git_transport_output`), but this reduces it once more to a
    constant allowlisted phrase from :func:`_redacted_git_failure_class`, falling back
    to a fixed label -- so whatever reaches the record is a constant, not free text.
    """
    text = str(err.get("error", "")) if isinstance(err, dict) else str(err)
    return _redacted_git_failure_class(text) or "clone failed"


def _read_clone_failure(entry: dict[str, Any]) -> float | None:
    """The epoch seconds a clone-level failure was last recorded for *entry*, or None.

    None means "retry": no record, a record older than :data:`_CLONE_FAILURE_BACKOFF`,
    or anything unreadable/malformed. The file sits in an agent-writable cache dir, so
    it is read ONCE through a no-follow descriptor (:func:`_read_pinned_sidecar`) whose
    opened inode is validated and whose bytes are bounded by the manifest ceiling --
    closing the size-check-then-read race a by-name ``stat``/``read_text`` had -- and is
    tolerant of garbage: a bad record reads as absent so the next prewarm re-clones and
    rewrites it. A record whose ``at`` is in the FUTURE (clock skew) is treated as
    fresh, not ignored, so a skewed clock cannot defeat the backoff.
    """
    path = _clone_failure_path(entry)
    result = _read_pinned_sidecar(path, max_bytes=_MANIFEST_MAX_BYTES)
    if result is None:
        return None
    data, _mtime = result
    try:
        recorded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(recorded, dict):
        return None
    at = recorded.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return None
    try:
        at = float(at)
    except OverflowError:
        # A huge int (``10**400``) is no timestamp this writer produced: absent.
        return None
    # The chained compare is False for NaN and both infinities.
    if not -1e18 < at < 1e18 or time.time() - at > _CLONE_FAILURE_BACKOFF:
        return None
    return at


def _write_clone_failure(entry: dict[str, Any], reason: str) -> None:
    """Record a clone-level failure for *entry* now, with a credential-free *reason*.

    Best-effort, same shape and atomic write as the unobtainable record: a failed write
    just leaves the row to be re-cloned next fetch. Overwrites any earlier record so the
    backoff window restarts from the latest failure."""
    path = _clone_failure_path(entry)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({"at": time.time(), "reason": reason}) + "\n")
    except OSError as exc:
        logger.debug(
            "store art of %s: clone-failure record not written: %s",
            entry.get("name", ""),
            exc,
        )


def _clear_clone_failure(entry: dict[str, Any]) -> None:
    """Remove *entry*'s clone-failure record; best-effort. Called when a later clone
    reaches the repo, so a dead-row record cannot outlive the failure it named."""
    try:
        _clone_failure_path(entry).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(
            "store art of %s: clone-failure record not cleared: %s",
            entry.get("name", ""),
            exc,
        )


def _store_assets_warm(entry: dict[str, Any], manifest: Any) -> bool:
    """True when nothing about *entry*'s store assets needs a clone right now.

    Warm means: a manifest cache younger than :data:`_REWARM_AGE` exists and every
    art path it declares either has its bytes in the blob cache or is recorded as
    unobtainable against that same manifest cache (:func:`_read_unobtainable_art`),
    so nothing another clone could produce is missing. *manifest* is that cached
    manifest when the caller has it (``_read_manifest_cache``, whose TTL is wider
    than :data:`_REWARM_AGE`, so nothing it refuses could have been warm); a
    missing or malformed manifest is never warm.
    """
    if not isinstance(manifest, dict) or not manifest:
        return False
    try:
        manifest_mtime = _manifest_cache_path(entry).stat().st_mtime
    except OSError:
        return False
    if time.time() - manifest_mtime > _REWARM_AGE:
        return False
    unobtainable = _read_unobtainable_art(entry, manifest_mtime)
    for asset in _declared_store_art(manifest):
        cache_path = _store_art_cache_path(entry, asset)
        if cache_path is None:
            # The proxy would refuse this path whatever we did; it does not make
            # the row cold.
            continue
        if cache_path.is_file() or asset in unobtainable:
            continue
        return False
    return True


def _open_pinned_asset_stat(
    root: Path, rel_parts: tuple[str, ...], *, max_bytes: int
) -> tuple[bytes, os.stat_result] | None:
    """Read *rel_parts* under *root* through pinned, no-follow descriptors, or None.

    The one validate-and-read tail behind :func:`_open_pinned_asset` (the art/manifest
    reader) and :func:`_read_pinned_sidecar` (the record reader): both want the bytes
    of a file opened once through a no-follow descriptor walk, and the sidecar reader
    additionally wants that same descriptor's ``st_mtime`` as the freshness it
    compares against the manifest cache. Returning the whole ``os.stat_result`` gives
    each caller what it needs from the SAME inode it read the bytes from, so there is
    one tail rather than two spellings of it that can drift.

    The by-path alternative -- ``(root / asset).resolve()`` then ``stat``, ``is_file``,
    a size check, and finally ``read_bytes`` by path -- is a TOCTOU: ``root`` is a
    throwaway clone in a same-uid, unmasked system tempdir the sandbox documents as
    agent-writable, so between the checks and the read a same-uid process can swap a
    path component for a symlink to e.g. ``~/.aws/credentials`` and the read follows
    it into the blob cache. The DIRECTORY chain is walked one descriptor at a time by
    :func:`pinned_fs.open_pinned_descendant_dir`, which refuses a link at every
    component (the root included); the final file is opened ``O_NOFOLLOW`` relative to
    the pinned leaf and its OPENED inode is validated (regular, single hard link,
    within *max_bytes*) before its bytes are read from the fd itself, so the bytes
    returned are the file the walk pinned and nothing swapped underneath.

    *rel_parts* are the segments of an already containment-validated relative path:
    an art path the caller gates through :func:`_store_art_cache_path` ->
    :func:`_servable_art_path` (only :data:`_SAFE_PATH_RE`, no ``..``, no leading
    ``/``, no hidden segment), the fixed ``("app.json",)`` for the manifest read
    itself, or the single record filename for a sidecar read -- none manufactures a
    traversing component. The last segment is the file; everything before it is the
    directory chain handed to the primitive (empty for a sidecar under its own
    parent).

    *max_bytes* is the size ceiling the opened descriptor is validated against, so the
    same pinned read serves the art files (:data:`_ART_MAX_BYTES`), the larger
    ``app.json`` (:data:`_MANIFEST_MAX_BYTES`) and the sidecar records; a file over it
    is a refusal, the same refusal an equivalent by-path size check would give.

    On a platform that cannot pin a walk (Windows: no ``dir_fd`` support) the primitive
    yields ``None`` and the leaf is addressed BY NAME; the final file is then
    ``lstat``-refused when a link/reparse point, opened, and its ``(st_dev, st_ino)``
    compared to the ``lstat`` so a component swapped between the walk and the open is
    caught. Same regular/single-link/size rules, same bytes-from-the-fd read.

    Returns ``(bytes, os.stat_result)``, or ``None`` for ANY source refusal: an empty
    path, a link at any component, a non-directory intermediate, a final that is not a
    regular file, a hard-linked final (``st_nlink != 1``), a file over *max_bytes*, or
    any ``OSError`` on the walk. A ``None`` return is exactly the source refusal the
    by-path ``OSError`` produced before, so callers record the asset unobtainable
    identically. Every descriptor is closed before returning.
    """
    if not rel_parts:
        return None
    name = rel_parts[-1]
    dir_parts = rel_parts[:-1]
    # O_NONBLOCK: a FIFO planted at the name would otherwise block ``os.open`` until a
    # writer appears, before the regular-file check below can refuse it.
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    win_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        with open_pinned_descendant_dir(root, dir_parts, what="store asset") as leaf:
            if leaf is not None:
                # POSIX: the file opens O_NOFOLLOW relative to the pinned leaf dir fd,
                # so a link at the final name is refused rather than followed.
                try:
                    fd = os.open(name, file_flags, dir_fd=leaf)
                except OSError:
                    return None
            else:
                # Windows by-name arm: lstat the final component, refuse a link/reparse
                # point, open it, and confirm the opened inode is the one lstat saw.
                final = Path(root).joinpath(*dir_parts, name)
                try:
                    lst = final.lstat()
                except OSError:
                    return None
                if is_reparse_point(final):
                    return None
                try:
                    fd = os.open(final, win_flags)
                except OSError:
                    return None
                try:
                    fst = os.fstat(fd)
                    # ``st_ino`` is 0 on some platforms/filesystems; when it is
                    # meaningful a mismatch means a swap between the walk and the open.
                    if lst.st_ino != 0 and (fst.st_dev, fst.st_ino) != (lst.st_dev, lst.st_ino):
                        os.close(fd)
                        return None
                except OSError:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    return None
            # One validate-and-read tail for both arms, on the OPENED descriptor: a
            # regular file, a single hard link (a file hard-linked elsewhere on the
            # host is refused), and no larger than *max_bytes*. The read is bounded at
            # ``max_bytes + 1`` so a file that grew past the fstat size is refused
            # rather than returned truncated.
            try:
                fst = os.fstat(fd)
                if not stat.S_ISREG(fst.st_mode):
                    return None
                if fst.st_nlink != 1:
                    return None
                if fst.st_size > max_bytes:
                    return None
                data = os.read(fd, max_bytes + 1)
                while len(data) <= max_bytes:
                    chunk = os.read(fd, max_bytes + 1 - len(data))
                    if not chunk:
                        break
                    data += chunk
                if len(data) > max_bytes:
                    return None
                return data, fst
            except OSError:
                return None
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
    except PinnedPathRefusal:
        # A link (or non-directory) at any directory component of the chain: the same
        # source refusal a by-path walk raised, recorded unobtainable by the caller.
        return None


def _open_pinned_asset(
    root: Path, rel_parts: tuple[str, ...], *, max_bytes: int | None = None
) -> bytes | None:
    """The bytes of *rel_parts* under *root*, read through a pinned no-follow walk.

    A thin wrapper over :func:`_open_pinned_asset_stat` that drops the ``os.stat_result``
    the sidecar readers need and returns only the bytes -- the shape the art and
    manifest reads want. Every refusal that function documents is a ``None`` here.
    """
    # Resolve the default at CALL time, not at def time: ``_ART_MAX_BYTES`` is a module
    # attribute tests monkeypatch, and a ``= _ART_MAX_BYTES`` parameter default would
    # freeze the value taken when this module was imported.
    if max_bytes is None:
        max_bytes = _ART_MAX_BYTES
    result = _open_pinned_asset_stat(root, rel_parts, max_bytes=max_bytes)
    if result is None:
        return None
    data, _st = result
    return data


def _read_pinned_sidecar(path: Path, *, max_bytes: int) -> tuple[bytes, float] | None:
    """Read a manifest-cache sidecar ONCE through a no-follow descriptor, or None.

    The ``.unobtainable``/``.clone-failed`` records live beside the manifest cache in
    the agent-writable ``by-source/`` directory, so a by-name ``stat`` (for the size
    ceiling) followed by a ``read_text`` is a TOCTOU: a same-uid process can swap the
    file for a symlink to something enormous, or grow it, between the size check and
    the read, so the read allocates past the ceiling the stat cleared. This reads the
    file through :func:`_open_pinned_asset_stat` -- the SAME validate-and-read tail the
    art/manifest reads use -- so the record is opened ONCE ``O_NOFOLLOW`` under its
    pinned parent directory, its OPENED descriptor is validated (a regular, single-link
    file no larger than *max_bytes*), and at most ``max_bytes + 1`` bytes are read from
    that same descriptor. Returns ``(bytes, mtime)`` where *mtime* is that descriptor's
    own ``st_mtime`` (the freshness the unobtainable record compares against the
    manifest cache, read from the same inode it read the bytes from), or ``None`` for
    any refusal: a link at the parent or the file, a non-regular or hard-linked file,
    one over the ceiling, a missing file, or any ``OSError``. The record is a single
    file directly under its parent, so ``rel_parts`` is just its name -- the primitive
    walks an empty directory chain and opens it under that pinned parent.
    """
    result = _open_pinned_asset_stat(path.parent, (path.name,), max_bytes=max_bytes)
    if result is None:
        return None
    data, st = result
    return data, st.st_mtime


def _publish_pinned_asset(cache_path: Path, data: bytes) -> None:
    """Write *data* to *cache_path* through a PINNED destination-parent descriptor.

    :func:`_store_art_cache_path` resolves ``cache_path`` and containment-checks it
    against the resolved blob-cache root, but a by-name ``cache_path.parent.mkdir``
    followed by ``atomic_write(cache_path, data)`` would re-walk that parent by name
    AFTER the check. Both the blob cache root and the tree it grows are agent-writable,
    so a same-uid process that swaps an intermediate parent for a symlink to an outside
    directory between the check and the write makes the copy overwrite an image-named
    file OUTSIDE the cache. This publishes through descriptors that cannot be
    re-pointed: :func:`pinned_fs.open_pinned_descendant_dir` opens :func:`config_dir`
    (Kiro Crew's owned data home, whose ancestors are not agent-writable) once and
    creates/opens ``cache``, ``blobs`` and each intermediate component with ``dir_fd``
    refusing a link (every component included, so a linked ``cache``/``blobs`` ancestor
    is refused rather than followed out of the data home), and the leaf is written with
    :func:`atomic_write`'s ``parent_dir_fd`` -- whose temp create and publishing rename
    are both ``dir_fd``-relative, so no component is re-resolved by name.

    The blob cache holds no secret, so no owner restriction is applied; the whole
    threat here is the write LANDING outside the cache, which the descriptor pin
    closes. Any refusal (a link at a component, an ``OSError`` on the walk or write)
    propagates as ``OSError`` -- the primitive is asked to raise ``OSError`` on
    refusal -- so the caller leaves the asset COLD, a destination-write failure
    retried on the next fresh fetch, never recorded unobtainable.

    On a platform without ``dir_fd`` support the primitive validates the parent chain
    by ``lstat`` (refusing a link/reparse/non-directory at the root and every
    component, creating the missing ones) and yields ``None``; the leaf is then
    written BY NAME with :func:`atomic_write`. The residual is the same swap window
    ``atomic_write`` documents for every by-name write on such a platform, which is
    also the one that cannot pin a directory at all.
    """
    blob_root = _blob_cache_dir()
    rel = cache_path.relative_to(blob_root)
    rel_dir_parts = rel.parts[:-1]
    # The whole chain -- ``cache/blobs`` AND every intermediate below it -- is opened
    # through ONE pinned walk anchored at :func:`config_dir`, Kiro Crew's owned data
    # home, whose ancestors are not agent-writable and so are the trusted anchor the
    # walk starts from. A by-name ``blob_root.mkdir(parents=True)`` before the walk
    # would create/follow ``config_dir()/cache/blobs`` BY NAME first, so a linked
    # ``cache`` (or ``blobs``) ancestor would be followed out of the data home before
    # the pinned walk ever ran. Opening ``("cache", "blobs", *rel_dir_parts)`` under
    # ``config_dir()`` instead refuses a link at ``cache``/``blobs`` and every
    # intermediate (creating the missing ones with ``dir_fd``), so no blob-cache
    # component is resolved by name.
    #
    # ``refusal=OSError``: a link at any parent component must reach the caller as the
    # OSError it treats as a destination-write failure, not the default refusal type.
    with open_pinned_descendant_dir(
        config_dir(),
        ("cache", "blobs", *rel_dir_parts),
        what="blob-cache parent",
        create=True,
        refusal=OSError,
    ) as leaf:
        if leaf is not None:
            atomic_write(cache_path, data, parent_dir_fd=leaf)
        else:
            atomic_write(cache_path, data)


def _copy_declared_art(
    entry: dict[str, Any],
    manifest_dir: Path,
    manifest: dict[str, Any],
    cancel: threading.Event | None = None,
) -> tuple[int, frozenset[str]]:
    """Copy every servable art file *manifest* declares into the blob cache.

    Blocking (file reads and writes) -- callers offload it. Returns the number of
    files written and the servable-shaped declared paths this call CONFIRMED the
    source could not supply, which it also records beside the manifest cache
    (:func:`_write_unobtainable_art`) so :func:`_store_assets_warm` stops asking for
    a clone that could not supply them. Each declared path is gated by
    :func:`_servable_art_path` through :func:`_store_art_cache_path`, then read
    through :func:`_open_pinned_asset`: a no-follow descriptor walk from the clone
    directory that refuses a link at any component and validates the same opened
    inode it reads from. A symlink anywhere on the asset's path -- including a
    declared asset that is itself a symlink inside the clone -- is therefore refused,
    not followed, closing the window in which a same-uid process swaps a path
    component in the agent-writable clone tempdir between a by-path check and the
    read. Non-regular files, hard-linked files, and files over :data:`_ART_MAX_BYTES`
    are refused.

    A path is recorded as unobtainable ONLY on a confirmed SOURCE refusal from the
    pinned read (absent, non-regular, hard-linked, oversize, a link at any
    component, or otherwise unreadable). A path :func:`_store_art_cache_path`
    rejects -- a non-image or a path outside the proxy's grammar -- is SKIPPED
    without being recorded: the proxy would refuse a request for it whatever the
    clone held, so it never makes the row cold and there is nothing to record. A
    failure to WRITE the blob cache is likewise not a source refusal: the asset
    stays cold (no cache file, not recorded), so the next fresh fetch retries it
    instead of treating a transient write refusal as a permanent one. Failures are
    per-file: one refused path never costs the row its other art.

    *cancel* lets the caller stop the copy cooperatively (checked before each
    asset). When it is set the copy returns at once WITHOUT recording anything --
    no ``.unobtainable`` sidecar is written -- because a partial run has not
    confirmed any path unobtainable. The caller awaits this return before removing
    the checkout, so a cancelled worker never records a not-yet-copied path against
    a manifest cache while its clone is being torn down.
    """
    unobtainable: set[str] = set()
    written = 0
    logged_lfs = False
    # One copy runs per fetched manifest snapshot (the warm check re-derives the same
    # capped list but never logs), so the overflow is reported once per snapshot.
    dropped = _dropped_list_art_paths(manifest)
    if dropped:
        logger.warning(
            "store art prewarm: %s declares more screenshots than one row keeps "
            "(cap %d per field); dropped %s -- those stay unavailable",
            entry.get("name") or entry.get("repo") or "?",
            _MAX_LIST_ART_PATHS_PER_FIELD,
            ", ".join(f"{key}={count}" for key, count in sorted(dropped.items())),
        )
    for asset in _declared_store_art(manifest):
        if cancel is not None and cancel.is_set():
            # Cancelled mid-copy: record nothing. A partial run has not established
            # any path as unobtainable, and writing a sidecar now would mark
            # not-yet-copied paths as confirmed-missing against the manifest cache.
            return written, frozenset()
        cache_path = _store_art_cache_path(entry, asset)
        if cache_path is None:
            logger.debug(
                "store art %r of %s is not a servable path; not cached",
                asset,
                entry.get("name", ""),
            )
            continue
        # Establish whether the SOURCE can supply this asset, through a pinned
        # no-follow walk of the already containment-validated relative path from the
        # manifest directory. A ``None`` return is a confirmed source refusal (a link
        # at any component, a non-regular/hard-linked/oversize final, or an OSError)
        # and marks the path unobtainable; a destination-write failure below must not.
        rel_parts = tuple(seg for seg in asset.split(posixpath.sep) if seg)
        data = _open_pinned_asset(manifest_dir, rel_parts)
        if data is None:
            logger.debug(
                "store art %r of %s could not be read from the clone (refused or absent)",
                asset,
                entry.get("name", ""),
            )
            unobtainable.add(asset)
            continue
        # A Git LFS-tracked image checks out as a tiny text POINTER under a plain
        # clone (the prewarm has no ``git lfs``), so publishing these bytes would
        # serve the pointer's ASCII as an ``<img>`` source -- a broken card, never
        # the picture. This is a second clearing path for the same "LFS is
        # unsupported" contract the publishing guide already states for authors:
        # detect the pointer and treat the asset as unobtainable (recorded, never
        # published), exactly like an absent one, so the row is not re-cloned for it
        # every fresh fetch. Logged once per row, not once per pointer, so a card
        # tracking several images through LFS is one line.
        if data.startswith(_LFS_POINTER_PREAMBLE):
            if not logged_lfs:
                logger.info(
                    "store art of %s declares Git LFS-tracked image(s); LFS is "
                    "unsupported, so they are treated as unobtainable rather than "
                    "publishing the pointer",
                    entry.get("name", ""),
                )
                logged_lfs = True
            unobtainable.add(asset)
            continue
        # The source is good. A failure to WRITE the blob cache is a transient
        # destination problem, NOT a source refusal: leave the asset cold (not
        # written, not recorded) so the next fresh fetch retries it. The publish
        # goes through a pinned destination-parent descriptor so a parent swapped
        # to a link cannot land these bytes outside the blob cache.
        try:
            _publish_pinned_asset(cache_path, data)
        except OSError as exc:
            logger.debug(
                "store art %r of %s could not be written to the blob cache: %s",
                asset,
                entry.get("name", ""),
                exc,
            )
            continue
        written += 1
    recorded = frozenset(unobtainable)
    _write_unobtainable_art(entry, recorded)
    return written, recorded


async def _fetch_owner_tier_store_assets(entry: dict[str, Any], registry_name: str) -> bool:
    """One owner-credentialed shallow clone of *entry*'s repository; cache its
    ``app.json`` and the art that manifest declares. True when the manifest landed.

    The caller has already established that *entry* is a row of a FRESH index of a
    build-pinned ``owner``-tier registry. This function re-checks what it can about
    the clone target itself -- supported transport, cloneable URL, trusted host,
    servable branch -- and records the credential grant in the SEL before cloning.
    """
    name = entry.get("name", "")
    git_url = _entry_git_url(entry)
    branch = entry.get("branch", "main")
    subdirectory = entry.get("subdirectory", "")
    if not isinstance(name, str) or not name or not git_url:
        return False
    if _git_target_is_unsupported(git_url) or not _looks_like_git_url(git_url):
        return False
    # Scheme allowlist, applied BEFORE any credential is offered. The trust gates
    # above are scheme-blind and ``_looks_like_git_url`` admits plaintext
    # ``http://``/``git://``, so an owner-tier row naming such a repo would be
    # cloned with owner credentials over an unauthenticated transport -- anything
    # on the network path could then read them. ``_is_supported_registry_transport``
    # is the same allowlist the registry index fetch applies (https, ssh/scp
    # ``git@host:``, or a bare name); a row it refuses stays cold.
    if not _is_supported_registry_transport(git_url):
        logger.warning(
            "store art prewarm skipped %s: unsupported clone transport %s",
            name,
            _strip_git_target_userinfo(git_url),
        )
        # A denied credential decision, mirroring the host-not-trusted refusal below:
        # this refusal also stops an owner-credentialed clone, so it leaves the same
        # SEL record an incident responder reads to see every stopped escalation.
        _sel_credential_decision(
            "prewarm_store_art_owner_tier",
            git_url,
            granted=False,
            reason="unsupported_transport",
        )
        return False
    if not isinstance(branch, str) or not _SAFE_BRANCH_RE.match(branch) or ".." in branch:
        logger.debug("store art prewarm skipped %s: unservable branch %r", name, branch)
        _sel_credential_decision(
            "prewarm_store_art_owner_tier",
            git_url,
            granted=False,
            reason="unservable_branch",
        )
        return False
    if not isinstance(subdirectory, str) or not _is_safe_registry_subdir(subdirectory):
        return False
    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        _sel_credential_decision(
            "prewarm_store_art_owner_tier",
            git_url,
            granted=False,
            reason="host_not_trusted",
        )
        return False

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-store-art-")
        # Owner posture: the same pair the install path grants after
        # ``_owner_tier_confirmed`` -- owner credentials plus the context sandbox
        # mode -- and the same audit record, under this path's own operation name
        # so the two grants stay distinguishable in the SEL.
        clone_env = await asyncio.to_thread(minimal_env)
        sandbox_mode = await asyncio.to_thread(_context_clone_sandbox_mode, git_url)
        await asyncio.to_thread(_sel_credential_grant, "prewarm_store_art_owner_tier", git_url)
        checkout_root = Path(tmp_root) / "branch"
        fetch_log: list[str] = []
        # ``mask_local_git_config`` is the prewarm's own hardening: the local
        # checkout steps run with system/global git config out of scope, so no
        # operator-configured ``filter.<name>.smudge`` program resolves for a
        # driver this repository selects. The install path does NOT mask (the same
        # masking disables Git LFS); this throwaway checkout is only ever read for
        # a few image files, so LFS-tracked art stays a pointer here and is simply
        # recorded as unobtainable.
        err = await _git_fetch_branch(
            git_url,
            branch,
            checkout_root,
            fetch_log,
            clone_env=clone_env,
            sandbox_mode=sandbox_mode,
            mask_local_git_config=True,
        )
        if err is not None:
            logger.info(
                "store art prewarm: could not fetch %s from registry %r (%s)",
                name,
                registry_name,
                str(err.get("error", "")) if isinstance(err, dict) else str(err),
            )
            # A clone-LEVEL failure (dead repo, revoked credential, hanging forge)
            # writes no manifest and no unobtainable record, so without a marker the
            # row is cold on every fresh index fetch and the listing pays the full
            # clone attempt each interval. Record it (credential-free reason) so the
            # prewarm pre-filter backs the row off for _CLONE_FAILURE_BACKOFF. A
            # budget cancellation is NOT this path -- it raises CancelledError, caught
            # below and re-raised -- so a cancelled row records nothing.
            await asyncio.to_thread(_write_clone_failure, entry, _clone_failure_reason(err))
            return False
        # The clone reached the repo. Any earlier clone-failure record described a
        # failure that has now cleared, so remove it before it can back off a live row.
        await asyncio.to_thread(_clear_clone_failure, entry)
        # ``_contained_join`` resolves paths (filesystem walks), so it runs off the loop.
        manifest_dir = await asyncio.to_thread(_contained_join, checkout_root, subdirectory)
        if manifest_dir is None:
            return False
        # Read ``app.json`` through the SAME pinned no-follow descriptor walk the art
        # files use, not by path. ``app.json`` in a hostile clone can be a symlink to
        # any readable JSON on this host (e.g. ``~/.aws/credentials``) and whatever the
        # read returns is written into the agent-visible manifest cache; a by-path
        # ``resolve``/``stat``/``read_text`` is a TOCTOU a same-uid process can win by
        # swapping ``app.json`` for a symlink between the containment check and the
        # read. The pinned read refuses a link at any component, a non-regular or
        # hard-linked file, and one over ``_MANIFEST_MAX_BYTES`` (the size ceiling
        # validated on the opened descriptor, so a separate by-path ``os.stat`` is
        # unnecessary), so the oversize guard is folded in and no path is walked twice.
        raw = await asyncio.to_thread(
            _open_pinned_asset, manifest_dir, ("app.json",), max_bytes=_MANIFEST_MAX_BYTES
        )
        if raw is None:
            # Absent, a symlink/hard link/non-regular file, or over the manifest
            # ceiling -- all the by-path checks now collapse into one source refusal.
            return False
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            # A non-UTF-8 manifest is as unusable as malformed JSON; refuse it here so
            # the failure is a plain skip, not an exception mid-read.
            return False
        manifest = json.loads(content)
        if not isinstance(manifest, dict):
            return False
        # Persist before publishing: art copied for a manifest that never reached the
        # cache would make the row read warm while its listing stays bare.
        if not await asyncio.to_thread(_write_manifest_cache, entry, manifest):
            return False
        # The copy worker runs off the loop and cannot be interrupted mid-write, so
        # a budget cancellation is turned into a cooperative one: on cancellation we
        # SET the event and AWAIT the worker's return before the ``finally`` removes
        # the checkout, so the worker never records a not-yet-copied path against
        # the manifest cache while its clone is being torn down. The worker returns
        # promptly -- it checks the event before each asset -- and records nothing
        # when it was cancelled.
        cancel = threading.Event()
        copy_task = asyncio.ensure_future(
            asyncio.to_thread(_copy_declared_art, entry, manifest_dir, manifest, cancel)
        )
        try:
            # Shield the FIRST await too: an unshielded ``await copy_task`` would
            # forward the cancellation into the task, and a cancelled task raises
            # at once on every later await without the thread having returned --
            # the settle below would then wait for nothing.
            written, unobtainable = await asyncio.shield(copy_task)
        except asyncio.CancelledError:
            cancel.set()
            # The worker is still live (only this coroutine was cancelled); wait
            # for it to observe the flag and return before the cleanup runs.
            await asyncio.shield(copy_task)
            raise
        logger.debug(
            "store art prewarm: %s cached %d art file(s), %d declared path(s) unobtainable",
            name,
            written,
            len(unobtainable),
        )
        return True
    except (asyncio.TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
        logger.info(
            "store art prewarm: %s from registry %r failed: %s",
            name,
            registry_name,
            exc,
        )
        return False
    finally:
        if tmp_root:
            await _rmtree_force_settled(tmp_root)


#: Concurrent owner-credentialed clones the prewarm runs. Shallow single-branch
#: clones of app repositories are small (measured: ~0.4 s, under 2 MiB each), so a
#: catalog of fifty rows warms in a few seconds at this width without turning a
#: refresh into fifty simultaneous ssh sessions against one forge.
_PREWARM_CONCURRENCY = 4

#: Seconds the whole prewarm batch may take. The prewarm runs inline on the listing
#: request that missed the index cache, so 20 s is the CANCELLATION deadline for the
#: batch's clones, not a hard ceiling on the wait: the batch runs as one gather task
#: waited on by ``asyncio.wait({batch}, timeout=...)``, and at the deadline the batch
#: is cancelled and its cleanup is settled in a SEPARATE bounded phase
#: (:data:`_PREWARM_CLEANUP_BUDGET`), so the request may additionally wait up to that
#: budget for the cancelled clones' process-group kill and scratch-dir removal
#: (``_fetch_owner_tier_store_assets``'s ``finally`` -> ``_rmtree_force_settled``); a
#: cleanup still running then is DETACHED so the request returns. Splitting the two
#: budgets is deliberate: a single ``asyncio.wait_for`` over the gather would cancel
#: the workers at the deadline and then await their unbounded cleanup INSIDE that same
#: call, so a hanging forge on a slow filesystem would stall the listing past the
#: budget with no bound. Rows still cold when the budget runs out stay cold until the
#: next fresh fetch; cancelling their in-flight clone is safe because ``checkout``'s
#: git runner kills the process group on ``CancelledError`` and
#: :func:`_fetch_owner_tier_store_assets` removes its scratch clone in ``finally``.
_PREWARM_BATCH_BUDGET = 20.0


#: Seconds the batch's post-budget CANCELLATION and cleanup phase may take before the
#: listing request stops waiting for it. Once :data:`_PREWARM_BATCH_BUDGET` fires, the
#: in-flight clones are cancelled and each ``_fetch_owner_tier_store_assets`` runs its
#: ``finally`` -> :func:`_rmtree_force_settled`: a process-group kill (whose own grace
#: is ``checkout._KILL_GRACE_PERIOD``, currently 5 s) followed by a scratch-dir
#: ``rmtree``. That settle is otherwise UNBOUNDED, so a hanging forge plus a slow
#: filesystem turns a cold App Store load into a multi-tens-of-seconds stall on top of
#: the batch budget. This bounds it: the cleanup phase gets its own budget, and on
#: expiry the still-running cleanup is DETACHED into a background task so the request
#: returns. Set to twice the process-kill grace -- the floor is ``_KILL_GRACE_PERIOD``
#: (a single kill grace), and one clone's kill-then-rmtree can legitimately take a bit
#: more than one grace, so a value below it would abandon cleanups that were about to
#: finish; well above it just delays the return with nothing left to wait on.
_PREWARM_CLEANUP_BUDGET = float(2 * _KILL_GRACE_PERIOD)


#: Cleanup tasks detached when :data:`_PREWARM_CLEANUP_BUDGET` expires, held so the
#: event loop does not garbage-collect a still-running task (``asyncio`` keeps only a
#: weak reference to a bare ``create_task`` result). Each task removes itself on
#: completion. The detached work is idempotent -- ``_rmtree_force_settled`` tolerates
#: an already-removed scratch dir under the system tempdir -- so a detached cleanup
#: finishing after the request returned is safe, and a test can await this set to
#: observe the finish.
_PENDING_PREWARM_CLEANUPS: set[asyncio.Task[Any]] = set()


def _detach_prewarm_cleanup(unfinished: asyncio.Future[Any]) -> None:
    """Move a still-running post-budget cleanup off the request path.

    *unfinished* is the gather of the batch's workers, cancelled at the batch budget
    and still settling its clones' cleanup when :data:`_PREWARM_CLEANUP_BUDGET`
    expired. Wrapping it in a background task, retained in :data:`_PENDING_PREWARM_CLEANUPS`
    until it completes, lets the listing request return while the cleanup finishes on
    its own. Logged once so a persistently slow cleanup is visible.
    """

    async def _finish() -> None:
        try:
            await unfinished
        except (asyncio.CancelledError, Exception):
            # The workers were already cancelled; this only drains their cleanup.
            # Any residue (a cancellation re-raise, an OSError from a torn-down
            # scratch dir) is nothing the returned request can act on.
            pass

    task = asyncio.ensure_future(_finish())
    _PENDING_PREWARM_CLEANUPS.add(task)
    task.add_done_callback(_PENDING_PREWARM_CLEANUPS.discard)
    logger.warning(
        "store art prewarm: cleanup exceeded its %.0fs budget; detached to finish in the "
        "background so the listing request returns",
        _PREWARM_CLEANUP_BUDGET,
    )


#: Rows one prewarm batch will CONSIDER for a clone. An owner-tier index is external
#: input and can list thousands of rows; retaining every one and creating a task per
#: row before the budget fires (the old ``todo.append`` + ``asyncio.gather`` shape)
#: grows memory with the index size, not with what a batch can actually finish. What
#: a batch CAN finish is bounded anyway: :data:`_PREWARM_BATCH_BUDGET` divided by the
#: per-clone cost (measured ~0.4 s) at :data:`_PREWARM_CONCURRENCY` width is on the
#: order of two hundred clones, so a cap of 200 is at or above the reachable ceiling
#: and never leaves a row cold that the batch would otherwise have warmed. Rows past
#: the cap stay cold: every batch examines the index in order and stops at the same
#: cap, so a stable tail past it is never reached by a later fetch either (a row
#: only moves into range when the publisher reorders or shrinks the index). The
#: overflow is logged once per batch with the index size and the cap, so a
#: persistently oversized index is visible rather than silently truncated.
_PREWARM_MAX_ROWS = 200


#: The row fields the prewarm retains per candidate -- everything
#: :func:`_fetch_owner_tier_store_assets` and the cache-path/identity helpers it
#: calls read, and nothing else. An external index row can carry a large
#: ``description`` and other display fields the prewarm never touches; retaining the
#: whole row for every candidate holds all of that in memory for the batch's life, so
#: a slim projection is built instead (see :func:`_slim_prewarm_row`).
_PREWARM_ROW_FIELDS = ("name", "gitUrl", "repo", "branch", "subdirectory", "commit", "_registry")


#: Ceiling on any single retained prewarm-row field. Every field in
#: :data:`_PREWARM_ROW_FIELDS` is a scalar string coordinate (a git URL, a repo key, a
#: branch, a name), and a real one is well under this. An owner-tier index is external
#: input, so a row can pad a retained field to an arbitrary length; retaining it holds
#: that whole string in memory for the batch's life and feeds it to a cache key or a
#: clone argv. A retained field that is non-string or longer than this makes
#: :func:`_slim_prewarm_row` REJECT the row (it never earns a clone), which bounds the
#: per-row footprint deterministically before the row reaches ``todo``.
_PREWARM_FIELD_MAX_LEN = 2048


def _slim_prewarm_row(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Project *entry* to the fields a prewarm clone needs, or ``None`` to reject it.

    :func:`_fetch_owner_tier_store_assets` reads ``name``/``gitUrl``/``repo``/
    ``branch``/``subdirectory``, and the manifest-cache and blob-cache-path helpers
    it calls read the same coordinates plus ``commit`` (folded into the manifest
    cache identity) and ``_registry`` (the fetch's own tagging check). Nothing else
    a row may carry -- a multi-kilobyte ``description``, screenshots metadata -- is
    read on the prewarm path, so it is dropped rather than retained for the batch's
    lifetime. A field the row does not have is simply absent from the projection,
    which every downstream reader already tolerates with a default.

    Each retained field is a scalar string, so a PRESENT one that is non-string or
    longer than :data:`_PREWARM_FIELD_MAX_LEN` makes this return ``None`` -- the row is
    rejected and never appended to ``todo``, rather than retained with an unbounded
    string that would sit in memory for the batch's life and feed a cache key or a
    clone argv. A missing field is not a rejection: the projection just omits it.
    """
    slim: dict[str, Any] = {}
    for key in _PREWARM_ROW_FIELDS:
        if key not in entry:
            continue
        value = entry[key]
        if not isinstance(value, str) or len(value) > _PREWARM_FIELD_MAX_LEN:
            return None
        slim[key] = value
    return slim


def _prewarm_foreign_claims(registry_name: str) -> tuple[bool, list[str]]:
    """``(unresolvable, claims)``: every ``repo`` key a source OTHER than
    *registry_name* declares, read once.

    A thin wrapper over :func:`sources._repo_key_claims` -- the one counting core the
    blob proxy's :func:`sources._repo_key_owner_count` also reads, so the two gates
    consult the SAME source union (the bundled registry file plus every configured
    external registry, ``ignore_ttl`` -- never fetched) and cannot drift apart. The
    prewarm's two deltas are the only arguments it passes: ``strict=True`` (a sibling
    whose cache is ABSENT or unreadable is a POSSIBLE claimant, not a proven
    non-claimant, so it fails closed) and ``except_registry`` (the registry this
    prewarm is attributing the row to is the claimant, not a rival, so its own cache
    is skipped). The per-source grouping the proxy needs is flattened here into the
    single claims list the per-row lookup checks membership against, so a prewarm
    batch of N rows still costs one pass over the sources. ``unresolvable`` is True
    when the claims cannot be established (a sibling with no readable cache, or any
    read that raised); the caller then treats EVERY key as ambiguous.
    """
    unresolvable, sources = _repo_key_claims(strict=True, except_registry=registry_name)
    if unresolvable:
        return True, []
    return False, [repo for source in sources for repo in source]


def _prewarm_provenance_ambiguous(
    registry_name: str, repo: str, claims: tuple[bool, list[str]]
) -> bool:
    """True when *repo*'s provenance is not unambiguously this one registry's.

    The prewarm may warm a row only when THIS registry is the sole possible
    claimant of the ``repo`` key -- the same single-owner rule the blob proxy's
    credential carve-out enforces, but with a stricter reading of a sibling whose
    cache it cannot read. A sibling registry contributes ambiguity when its cache
    DECLARES the key, AND ALSO when its cache is ABSENT or unreadable: an unfetched
    or GC'd sibling could publish the same key, and a provenance that cannot be
    established must never buy an owner-credentialed clone. The bundled registry
    file counts as another declaring source. Any read failure fails closed
    (ambiguous). Only when every OTHER source has a readable cache that does NOT
    declare the key is the row unambiguous and warmable.

    *claims* is the batch's :func:`_prewarm_foreign_claims` result, read once per
    batch and passed to every row, so a batch of N rows costs one sibling-cache read.
    """
    unresolvable, claimed_repos = claims
    if unresolvable:
        return True
    return any(_same_git_target(claimed, repo) for claimed in claimed_repos)


async def _prewarm_owner_tier_store_assets(reg: Any, entries: list[dict[str, Any]]) -> int:
    """Warm the manifest and blob caches for the FRESH rows of an owner-tier registry.

    *entries* MUST be the list a fresh fetch of *reg*'s index just returned (see the
    module docstring: this is what makes the credential grant attributable to the
    build-pinned index rather than to an agent-writable cache). Both call sites --
    the listing path's cache miss and the explicit refresh -- pass exactly that.
    Any other registry tier returns 0 without a clone. Rows already warm are
    skipped; every failure is per-row and logged, never raised, because the store
    listing that triggered the fetch must render whatever art did land. The batch as
    a whole is bounded by :data:`_PREWARM_BATCH_BUDGET`, for the same reason.

    Returns the number of rows whose manifest was fetched this call.
    """
    if not entries:
        return 0
    registry_name = _public_registry_name(reg)
    # Read the tier off the SAME row object whose index was just fetched, never by
    # re-resolving the name: config.json is agent-writable, so a name looked up
    # again could now point at a granted repository while *entries* came from an
    # index the agent chose.
    tier = await asyncio.to_thread(_registry_trust_tier_of, reg)
    if tier != _TRUST_OWNER:
        return 0

    # The provenance gate below judges every row against the SAME set of foreign
    # claims, so the sources (bundled registry file + each sibling's cache) are
    # read once here and each row is then a pure lookup.
    claims = await asyncio.to_thread(_prewarm_foreign_claims, registry_name)
    todo: list[dict[str, Any]] = []
    overflow = 0
    field_rejects = 0
    examined = 0
    for entry in entries:
        # Cap FIRST, before any per-row work, and on rows EXAMINED rather than rows
        # kept: an owner-tier index is external input and can list thousands of rows,
        # and the per-row provenance/cache/backoff reads below are I/O, so a cap that
        # counted only cold candidates would let an index of arbitrarily many warm rows
        # spend unbounded cache reads. Once the batch has examined as many rows as it
        # can finish within _PREWARM_BATCH_BUDGET, every remaining row is counted as
        # overflow and skipped without a read: they stay cold, and because every
        # batch walks the index in order from the top, a stable tail past the cap
        # stays cold on later fetches too (see _PREWARM_MAX_ROWS).
        if examined >= _PREWARM_MAX_ROWS:
            overflow += 1
            continue
        examined += 1
        if not isinstance(entry, dict):
            continue
        # A row tagged for another registry is not this fetch's row. The fetch
        # tags every row it returns, so a mismatch here is a caller error, and
        # the safe answer to a caller error about credentials is to do nothing.
        if entry.get("_registry") != registry_name:
            continue
        # Provenance gate. The blob proxy grants owner credentials for a ``repo``
        # key only when exactly ONE configured source claims it, because the entry
        # is selected by repo alone and is provenance-blind; prewarming an
        # ambiguous key's bytes into the blob cache would defeat that refusal, as a
        # request reachable through a DIFFERENT source for the same key would be
        # served these cached bytes before the proxy's gate ever runs -- a
        # cross-registry confused-deputy read. A sibling registry whose cache is
        # ABSENT or unreadable is counted as a POSSIBLE owner, not as a
        # non-claimant: a first-refresh or GC'd sibling that has not written its
        # cache yet could publish the same key, so a repo only this registry's
        # readable cache declares is not yet known to be unique. The row is
        # prewarmed only when every OTHER source has a readable cache that does not
        # declare the key. Skipped rows count as not fetched.
        repo = entry.get("repo", "")
        # Deny by default: a row whose repo key is missing, empty or not a string
        # cannot state a claim at all, so it never earns the owner-credentialed
        # clone -- exactly the row the ambiguity gate exists to refuse.
        if not isinstance(repo, str) or not repo:
            logger.warning(
                "store art prewarm: skipping %s (no usable repo key)", entry.get("name", "")
            )
            # A denied credential decision, same helper as every other refusal on this
            # path, so a stopped escalation leaves an audit record whatever gate stops
            # it. The clone URL is recorded when the row carries one.
            _sel_credential_decision(
                "prewarm_store_art_owner_tier",
                _entry_git_url(entry),
                granted=False,
                reason="no_repo_key",
            )
            continue
        if _prewarm_provenance_ambiguous(registry_name, repo, claims):
            logger.warning(
                "store art prewarm: skipping %s (repo key provenance ambiguous "
                "across configured sources)",
                entry.get("name", ""),
            )
            _sel_credential_decision(
                "prewarm_store_art_owner_tier",
                _entry_git_url(entry),
                granted=False,
                reason="ambiguous_provenance",
            )
            continue
        cached = await asyncio.to_thread(_read_manifest_cache, entry)
        if await asyncio.to_thread(_store_assets_warm, entry, cached):
            continue
        # A recent clone-LEVEL failure backs the row off: a cold row whose last clone
        # failed outright left no manifest to warm-check, so without this it would be
        # re-cloned with owner credentials on every fresh index fetch. A record older
        # than _CLONE_FAILURE_BACKOFF (or malformed) reads as absent, so the row is
        # retried once the window lapses.
        failed_at = await asyncio.to_thread(_read_clone_failure, entry)
        if failed_at is not None:
            logger.debug(
                "store art prewarm: skipping %s (clone failed %.0fs ago, backing off)",
                entry.get("name", ""),
                time.time() - failed_at,
            )
            continue
        # Retain only the fields a fetch and its caches read, not the whole row: an
        # index row can carry a large description the prewarm never touches. A row
        # whose retained field is non-string or over _PREWARM_FIELD_MAX_LEN is
        # rejected here (never appended), so a padded field cannot sit in memory for
        # the batch's life or feed a cache key/clone argv.
        slim = _slim_prewarm_row(entry)
        if slim is None:
            field_rejects += 1
            continue
        todo.append(slim)
    if field_rejects:
        logger.warning(
            "store art prewarm: registry %r skipped %d row(s) with a non-string or "
            "over-length retained field (cap %d)",
            registry_name,
            field_rejects,
            _PREWARM_FIELD_MAX_LEN,
        )
    if overflow:
        # Report the INDEX size (rows examined plus rows skipped past the cap) and the
        # cap, not the number of clone candidates: the candidates are the cold subset
        # of the examined rows and say nothing about how oversized the index is.
        logger.warning(
            "store art prewarm: registry %r index has more rows than one batch "
            "considers (%d rows, capped at %d); the %d past the cap stay cold on every "
            "batch while the index keeps this order",
            registry_name,
            examined + overflow,
            _PREWARM_MAX_ROWS,
            overflow,
        )
    if not todo:
        return 0

    # A FIXED pool of at most _PREWARM_CONCURRENCY workers pulls rows from a queue,
    # so the number of in-flight clone coroutines never exceeds the concurrency width
    # regardless of how many rows the batch holds -- the old "one task per row, gated
    # by a semaphore" shape created every task up front, holding a coroutine per row
    # even though only _PREWARM_CONCURRENCY could run. Rows are recorded as they land,
    # so the count is right whether the batch runs to completion or the budget cancels
    # the workers still in flight. A list, not a rebound counter: the registry facade
    # forbids ``nonlocal``/``global`` rebinds.
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for entry in todo:
        queue.put_nowait(entry)
    landed: list[str] = []

    async def _worker() -> None:
        while True:
            try:
                entry = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                ok = await _fetch_owner_tier_store_assets(entry, registry_name)
            except asyncio.CancelledError:
                # Budget cancellation: let it propagate so the batch unwinds and the
                # in-flight clone's own cleanup runs.
                raise
            except BaseException as exc:  # per-row failure, logged not raised
                logger.warning(
                    "store art prewarm for %s from registry %r raised: %s",
                    entry.get("name", ""),
                    registry_name,
                    exc,
                )
                continue
            if ok:
                landed.append(entry.get("name", ""))

    worker_count = min(_PREWARM_CONCURRENCY, len(todo))
    # The workers run as one gather task so the batch budget and the post-budget
    # cleanup can be bounded SEPARATELY. ``asyncio.wait_for(gather, budget)`` would
    # cancel the workers at the deadline and then await their cleanup to settle
    # INSIDE that same call -- and each ``_fetch_owner_tier_store_assets`` finally
    # runs ``_rmtree_force_settled`` (a process-group kill plus a scratch ``rmtree``),
    # which is unbounded, so a hanging forge on a slow filesystem stalls the listing
    # request for tens of seconds past the budget. Instead: wait the batch budget for
    # the workers, then, if they are still running, cancel them and wait only
    # ``_PREWARM_CLEANUP_BUDGET`` for the cleanup to settle; a cleanup still running
    # then is DETACHED to finish in the background so the request returns now. The
    # detached ``rmtree`` is idempotent over a system-tempdir scratch dir, so
    # finishing later is safe.
    workers = asyncio.gather(*(_worker() for _ in range(worker_count)))
    batch = asyncio.ensure_future(workers)
    try:
        await asyncio.wait({batch}, timeout=_PREWARM_BATCH_BUDGET)
    except asyncio.CancelledError:
        # The whole prewarm coroutine was cancelled from OUTSIDE (a gateway
        # shutdown mid cold load), not the batch budget elapsing. ``asyncio.wait``
        # does NOT cancel the futures it waits on when it is itself cancelled, so
        # ``batch`` -- and its up-to-``_PREWARM_CONCURRENCY`` owner-credentialed
        # clones with their scratch dirs -- would be left running detached with no
        # one to settle them. Cancel it and let its workers' cleanup
        # (``_fetch_owner_tier_store_assets``'s ``finally`` -> process-group kill +
        # ``rmtree``) settle before propagating, bounded by the same
        # ``_PREWARM_CLEANUP_BUDGET`` the budget path uses so a hanging forge cannot
        # hold shutdown open. ``asyncio.wait({batch}, ...)`` -- not
        # ``wait_for(shield(batch), ...)`` -- because the settling batch re-raises
        # ``CancelledError`` as it unwinds, and ``asyncio.wait`` reports that by the
        # task's absence from ``done`` rather than raising it, so it is not confused
        # with a fresh cancellation of this handler. A cleanup still running at the
        # deadline is DETACHED so shutdown is not held open. This path re-raises the
        # ORIGINAL cancellation regardless.
        batch.cancel()
        done, _pending = await asyncio.wait({batch}, timeout=_PREWARM_CLEANUP_BUDGET)
        if batch not in done:
            _detach_prewarm_cleanup(batch)
        raise
    if batch.done():
        # Surface a non-cancellation crash of the gather itself; per-row failures are
        # already swallowed inside ``_worker``, so this is only a programming error.
        batch.result()
        fetched = len(landed)
        logger.info(
            "store art prewarm: registry %r fetched %d of %d row(s)",
            registry_name,
            fetched,
            len(todo),
        )
        return fetched

    # Budget elapsed with workers still in flight: cancel them, then bound the
    # cleanup settle. ``asyncio.wait({batch}, timeout=...)`` -- not
    # ``asyncio.wait_for(asyncio.shield(batch), ...)`` -- because the settling batch
    # re-raises ``CancelledError`` as it unwinds, and ``wait_for`` surfaces that as a
    # ``CancelledError`` INDISTINGUISHABLE from one delivered to THIS task from
    # outside: a bare ``except CancelledError: pass`` would then silently swallow a
    # real outer cancellation. ``asyncio.wait`` never raises the awaited task's own
    # ``CancelledError``, so the batch's unwind is reported by its absence from
    # ``done`` while an OUTER cancellation propagates out of the ``await`` naturally.
    # A cleanup still running at the deadline (``batch`` not in ``done``) is DETACHED
    # so the request returns now.
    batch.cancel()
    done, _pending = await asyncio.wait({batch}, timeout=_PREWARM_CLEANUP_BUDGET)
    if batch not in done:
        _detach_prewarm_cleanup(batch)
    logger.warning(
        "store art prewarm: registry %r exceeded its %.0fs batch budget; "
        "%d row(s) fetched, %d left cold until the next fresh fetch",
        registry_name,
        _PREWARM_BATCH_BUDGET,
        len(landed),
        len(todo) - len(landed),
    )
    return len(landed)
