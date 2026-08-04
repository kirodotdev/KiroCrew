"""Shared helpers for building the KiroCrew website frontend assets.

The canonical frontend lives **in-tree** at ``<repo-root>/website`` (a Vite +
React app). Its ``npm run build`` output lands in ``<repo-root>/website/dist``
and must be staged into ``<repo-root>/src/kiro_crew/static/dist`` so the
gateway can serve the SPA. Everything here operates on that in-tree layout.

For backwards compatibility with side-by-side dev checkouts, a *sibling*
``KiroCrewWebsite/dist`` clone is honored as a last-resort fallback when
resolving an already-built dist at runtime (see ``ensure_dev_dist_symlink``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

# The frontend is in-tree at ``<repo-root>/website``; there is no remote to
# clone. ``KIROCREW_WEBSITE_REPO`` is retained only so existing tooling/docs
# referencing the public mirror keep a stable name to point at.
_DEFAULT_REPO_URL = "https://github.com/kirodotdev/KiroCrew"
_REPO_URL = os.environ.get("KIROCREW_WEBSITE_REPO") or _DEFAULT_REPO_URL
# In-tree frontend directory name (under the repo root). The legacy sibling
# clone directory name is kept only for last-resort dist resolution.
_DIR_NAME = "website"
_SIBLING_DIR_NAME = "KiroCrewWebsite"

# Build timeouts (seconds). npm installs/builds can be slow on cold caches.
_INSTALL_TIMEOUT = 300
_BUILD_TIMEOUT = 300

# Env vars that select the frontend EDITION composition root (see
# ``website/vite.config.ts`` ``editionExtensionPlugin`` and
# ``website/docs/extension-seams.md``). ``KIROCREW_EDITION_DIR`` names the
# edition's own ``extensions.tsx``; ``KIROCREW_ALLOW_EDITION=1`` is the
# fail-closed opt-in that must accompany it.
_EDITION_DIR_ENV = "KIROCREW_EDITION_DIR"
_EDITION_OPT_IN_ENV = "KIROCREW_ALLOW_EDITION"
# Composition-root filenames ``editionExtensionPlugin`` accepts, in its order.
_EDITION_ENTRIES = ("extensions.tsx", "extensions.ts")


def edition_sources_missing() -> bool:
    """True when an edition dir is configured but its composition root is gone.

    ``vite.config.ts`` resolves the entry EAGERLY and throws when the dir holds no
    ``extensions.tsx``/``.ts``, deliberately: a silent degrade would ship an
    edition build with none of its edition behavior. That is the right call at
    build time and the wrong outcome for a RUNTIME rebuild, where the same
    condition is routine — a packaged install (wheel or bundle) ships the built
    ``dist`` but not the edition's TypeScript sources.

    Rebuilding there can only produce a stock SPA staged over the edition
    dashboard, so the caller SKIPS instead, leaving the shipped bundle in place.
    Absent ``KIROCREW_EDITION_DIR`` this is ``False`` and the stock path is
    untouched.
    """
    edition_dir = os.environ.get(_EDITION_DIR_ENV)
    if not edition_dir:
        return False
    root = Path(edition_dir)
    return not any((root / name).is_file() for name in _EDITION_ENTRIES)


def _edition_build_env() -> Optional[dict[str, str]]:
    """Environment for ``npm run build``, or ``None`` to inherit unchanged.

    The runtime rebuild (``POST /api/update``, ``kirocrew update``, and the
    gateway's auto-apply) shells ``npm run build`` in the SAME checkout the
    edition was built from. Vite reads the edition seam from the environment, so
    an inherited-but-incomplete environment decides which edition gets built —
    and both failure modes are silent:

    A downstream edition sets both vars in its own build script. If the rebuild
    dropped them, it would compile the STOCK SPA over the served ``static/dist``
    and silently replace the edition dashboard with upstream's.

    **The opt-in is READ, never synthesized.** ``KIROCREW_ALLOW_EDITION=1`` is the
    fail-closed gate on compiling an edition's proprietary sources into
    ``website/dist``, which is staged into the packaged wheel — a published
    release cannot be unpublished, so that is a one-way door and
    ``website/AGENTS.md`` says never to set the opt-in outside the edition's own
    build. Forcing it here would defeat exactly that gate: an edition dir left in
    the environment without the opt-in would start producing edition-composed
    packaged data instead of failing closed. So this returns ``None`` unless the
    operator's own environment carries the opt-in, and vite's
    ``KIROCREW_EDITION_DIR``-without-opt-in error still fires when it should.

    Returning ``None`` also keeps the stock path byte-identical to inheriting
    ``os.environ`` — the common case allocates nothing and changes nothing.
    """
    edition_dir = os.environ.get(_EDITION_DIR_ENV)
    if not edition_dir:
        return None
    if os.environ.get(_EDITION_OPT_IN_ENV) != "1":
        # Fail closed, deliberately: let vite raise its own explicit error rather
        # than manufacturing consent to compile edition sources into the package.
        return None
    env = dict(os.environ)
    env[_EDITION_DIR_ENV] = edition_dir
    env[_EDITION_OPT_IN_ENV] = "1"
    return env


def _repo_root(kiro_crew_pkg_dir: Path) -> Path:
    """Return the repo root given the ``kiro_crew`` package directory.

    Layout: ``<repo-root>/src/kiro_crew/`` is *kiro_crew_pkg_dir*, so two
    ``.parent`` hops land on the repo root (parent of ``src/``).
    """
    return kiro_crew_pkg_dir.parent.parent


def _resolve_website_dist(kiro_crew_pkg_dir: Path) -> Optional[Path]:
    """Locate a usable, already-built ``dist`` without touching the filesystem.

    Probes, in order:

    1. The in-tree build — ``<repo-root>/website/dist`` (the canonical
       location populated by ``npm run build``).
    2. A sibling checkout — ``<repo-root>/../KiroCrewWebsite/dist`` (legacy
       side-by-side dev layout). Last-resort only.

    Returns the resolved dist path on success, ``None`` otherwise.
    """
    repo_root = _repo_root(kiro_crew_pkg_dir)

    # 1. In-tree website/dist (canonical).
    in_tree_dist = repo_root / _DIR_NAME / "dist"
    if in_tree_dist.is_dir() and (in_tree_dist / "index.html").is_file():
        return in_tree_dist.resolve()

    # 2. Sibling KiroCrewWebsite/dist (legacy fallback).
    sibling_dist = repo_root.parent / _SIBLING_DIR_NAME / "dist"
    if sibling_dist.is_dir() and (sibling_dist / "index.html").is_file():
        return sibling_dist.resolve()

    return None


def ensure_dev_dist_symlink() -> Optional[Path]:
    """Make the website React build discoverable at runtime.

    The dashboard serves its SPA from ``<kiro_crew>/static/dist/index.html``.
    A ``pip``/wheel install ships that directory pre-bundled (the npm build
    output is committed/packaged into the wheel). That path does not fire on a
    plain source-tree run (``PYTHONPATH=src python -m kiro_crew gateway``,
    ``dev-backend.sh``, etc.), so without this the gateway has no SPA bundle
    and serves the "not found" guidance page.

    This helper reconciles the gap at gateway start:

    1. Existing real directory with ``index.html`` → no-op (packaged install /
       a prior local build that populated the source tree / manual setup).
    2. Existing symlink → validated; dangling or empty targets get replaced.
    3. Missing → resolve the in-tree ``website/dist`` (or a sibling
       ``KiroCrewWebsite`` checkout as a last resort) and symlink to it.

    Symlink over copy: no source-tree churn, ``.gitignore`` already excludes
    ``static/dist/``, and a fresh ``website`` rebuild propagates to the gateway
    with no extra step.

    Returns the resolved dist path on success, ``None`` if nothing could be
    found (caller should warn; the gateway then serves the "not built"
    guidance page — there is no legacy dashboard fallback).
    """
    kiro_crew_pkg_dir = Path(__file__).resolve().parent
    tree_dist = kiro_crew_pkg_dir / "static" / "dist"

    # A prior run may have created a symlink (POSIX) OR a directory junction
    # (non-admin Windows); both are "links" here and neither is a real dir.
    tree_dist_is_link = platform_compat.is_link_or_junction(tree_dist)

    # Case 1: real directory already populated (packaged install / a prior
    # local build landing in the source tree / user ran kirocrew init --ui).
    if tree_dist.is_dir() and not tree_dist_is_link:
        if (tree_dist / "index.html").is_file():
            return tree_dist
        # Empty real dir — fall through and try to resolve something usable.

    # Case 2: existing link — validate and re-use if the target still has
    # a dist in it. A dangling or empty target means the website build moved
    # or was cleaned; drop the link and re-resolve below.
    if tree_dist_is_link:
        try:
            target = tree_dist.resolve(strict=True)
        except (FileNotFoundError, OSError):
            target = None
        if target is not None and (target / "index.html").is_file():
            return target
        try:
            platform_compat.unlink_link_or_junction(tree_dist)
        except OSError as exc:
            logger.warning("Failed to remove stale dist link %s: %s", tree_dist, exc)
            return None

    # Case 3: no usable dist in place — probe and link.
    candidate = _resolve_website_dist(kiro_crew_pkg_dir)
    if candidate is None:
        return None

    tree_dist.parent.mkdir(parents=True, exist_ok=True)
    # Guard against a lingering empty real dir from Case 1's fall-through, or a
    # stale link/junction (rmtree must never descend THROUGH a link).
    if tree_dist.exists() or platform_compat.is_link_or_junction(tree_dist):
        try:
            if tree_dist.is_dir() and not platform_compat.is_link_or_junction(tree_dist):
                shutil.rmtree(tree_dist)
            else:
                platform_compat.unlink_link_or_junction(tree_dist)
        except OSError as exc:
            logger.warning("Failed to clear %s before linking: %s", tree_dist, exc)
            return None
    try:
        # symlink on POSIX; directory junction on non-admin Windows, where a
        # plain symlink needs SeCreateSymbolicLinkPrivilege and would fail with
        # WinError 1314 — leaving a source-tree gateway with no SPA bundle.
        platform_compat.symlink_or_junction(str(candidate), str(tree_dist))
    except OSError as exc:
        logger.warning("Failed to link %s -> %s: %s", tree_dist, candidate, exc)
        return None
    logger.info("Linked frontend dist: %s -> %s", tree_dist, candidate)
    return candidate


def _stage_dist(
    built_dist: Path,
    proj_path: Path,
    log: Callable[[str], None] = print,
) -> None:
    """Copy the freshly built ``website/dist`` into ``static/dist``.

    Removes any stale destination (real dir or symlink) first, then copies the
    build output so the dashboard serves the latest frontend assets without a
    manual step. A copy (rather than a symlink) is used here so the served
    bundle is a self-contained snapshot independent of later ``website/``
    rebuilds — important for packaged/installed layouts.
    """
    static_dist = proj_path / "src" / "kiro_crew" / "static" / "dist"
    if not built_dist.is_dir():
        log(f"  ⚠️  Built dist not found at {built_dist} — dashboard may be stale")
        return
    try:
        if static_dist.is_symlink() or static_dist.is_file():
            static_dist.unlink()
        elif static_dist.is_dir():
            shutil.rmtree(static_dist)
    except OSError as exc:
        log(f"  ⚠️  Could not remove stale static/dist: {exc}")
        return
    try:
        static_dist.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(built_dist, static_dist)
        log(f"  📦 Staged static/dist ← {built_dist}")
    except OSError as exc:
        log(f"  ⚠️  Could not copy static/dist: {exc}")


def build_frontend_sync(
    proj_path: Path,
    log: Callable[[str], None] = print,
) -> None:
    """Build the in-tree ``website/`` frontend and stage it (synchronous).

    Runs ``npm ci`` (falling back to ``npm install`` when there is no
    lockfile) then ``npm run build`` in ``<proj>/website``, then copies
    ``website/dist`` into ``src/kiro_crew/static/dist``. Graceful no-op when
    there is no ``website/`` directory or ``npm`` is not installed.

    The edition seam is threaded through the build (see
    :func:`_edition_build_env`), so a downstream edition's rebuild recomposes THAT
    edition rather than staging a stock bundle over it.
    """
    website_dir = proj_path / _DIR_NAME
    if not website_dir.is_dir():
        log("  ⚠️  No website/ directory — skipping frontend build")
        return
    # Resolve to a full path: on Windows npm is ``npm.CMD``, which PATHEXT-aware
    # shutil.which finds but CreateProcess cannot spawn by the bare name "npm".
    npm = shutil.which("npm")
    if not npm:
        log("  ⚠️  npm not found — skipping frontend build")
        return
    if edition_sources_missing():
        log("  ⚠️  Edition frontend sources not present — keeping the shipped dashboard")
        return

    log("  🔨 Building frontend (npm)…")
    install_args = (
        ["ci", "--no-audit", "--no-fund"]
        if (website_dir / "package-lock.json").is_file()
        else ["install", "--no-audit", "--no-fund"]
    )
    try:
        r = subprocess.run(
            [npm, *install_args],
            cwd=str(website_dir), capture_output=True, timeout=_INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log("  ⚠️  Frontend npm install timed out — dashboard may be stale")
        return
    if r.returncode != 0:
        log("  ⚠️  Frontend npm install failed — dashboard may be stale")
        return

    try:
        r = subprocess.run(
            [npm, "run", "build"],
            cwd=str(website_dir), capture_output=True, timeout=_BUILD_TIMEOUT,
            env=_edition_build_env(),
        )
    except subprocess.TimeoutExpired:
        log("  ⚠️  Frontend build timed out — dashboard may be stale")
        return
    if r.returncode != 0:
        log("  ⚠️  Frontend build failed — dashboard may be stale")
        return

    _stage_dist(website_dir / "dist", proj_path, log)


async def build_frontend_async(
    proj: str,
    push_progress: Optional[Callable[[str, str], None]] = None,
) -> None:
    """Build the in-tree ``website/`` frontend and stage it (async).

    Async sibling of :func:`build_frontend_sync`: runs ``npm ci`` (fallback
    ``npm install``) then ``npm run build`` in ``<proj>/website`` with
    timeouts + kill-on-timeout, then copies ``website/dist`` into
    ``src/kiro_crew/static/dist``. Graceful no-op when there is no
    ``website/`` directory or ``npm`` is not installed.

    Threads the edition seam like the sync helper — this is the path
    ``POST /api/update`` and the gateway auto-apply take, so an edition install
    must not silently rebuild as stock here either.
    """
    proj_path = Path(proj)
    website_dir = proj_path / _DIR_NAME

    def _warn(msg: str) -> None:
        if push_progress:
            push_progress("warning", msg)

    if not website_dir.is_dir():
        _warn("No website/ directory -- skipping frontend build")
        return
    # Resolve to a full path: on Windows npm is ``npm.CMD``, which PATHEXT-aware
    # shutil.which finds but CreateProcess cannot spawn by the bare name "npm".
    npm = shutil.which("npm")
    if not npm:
        _warn("npm not found -- skipping frontend build")
        return
    if edition_sources_missing():
        _warn("Edition frontend sources not present -- keeping the shipped dashboard")
        return

    install_args = (
        ["ci", "--no-audit", "--no-fund"]
        if (website_dir / "package-lock.json").is_file()
        else ["install", "--no-audit", "--no-fund"]
    )
    npm_i = await asyncio.create_subprocess_exec(
        npm, *install_args,
        cwd=str(website_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(npm_i.wait(), timeout=_INSTALL_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            npm_i.kill()
        except ProcessLookupError:
            pass
        await npm_i.wait()
        _warn("Frontend npm install timed out -- dashboard may be stale")
        return
    if npm_i.returncode != 0:
        _warn("Frontend npm install failed -- dashboard may be stale")
        return

    npm_build = await asyncio.create_subprocess_exec(
        npm, "run", "build",
        cwd=str(website_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=_edition_build_env(),
    )
    try:
        await asyncio.wait_for(npm_build.wait(), timeout=_BUILD_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            npm_build.kill()
        except ProcessLookupError:
            pass
        await npm_build.wait()
        _warn("Frontend build timed out -- dashboard may be stale")
        return
    if npm_build.returncode != 0:
        _warn("Frontend build failed -- dashboard may be stale")
        return

    _stage_dist(website_dir / "dist", proj_path, log=lambda _m: None)
