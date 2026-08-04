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

    # Case 1: real directory already populated (packaged install / a prior
    # local build landing in the source tree / user ran kirocrew init --ui).
    if tree_dist.is_dir() and not tree_dist.is_symlink():
        if (tree_dist / "index.html").is_file():
            return tree_dist
        # Empty real dir — fall through and try to resolve something usable.

    # Case 2: existing symlink — validate and re-use if the target still has
    # a dist in it. A dangling or empty target means the website build moved
    # or was cleaned; drop the link and re-resolve below.
    if tree_dist.is_symlink():
        try:
            target = tree_dist.resolve(strict=True)
        except (FileNotFoundError, OSError):
            target = None
        if target is not None and (target / "index.html").is_file():
            return target
        try:
            tree_dist.unlink()
        except OSError as exc:
            logger.warning("Failed to remove stale dist symlink %s: %s", tree_dist, exc)
            return None

    # Case 3: no usable dist in place — probe and link.
    candidate = _resolve_website_dist(kiro_crew_pkg_dir)
    if candidate is None:
        return None

    tree_dist.parent.mkdir(parents=True, exist_ok=True)
    # Guard against a lingering empty real dir from Case 1's fall-through.
    if tree_dist.exists() or tree_dist.is_symlink():
        try:
            if tree_dist.is_dir() and not tree_dist.is_symlink():
                shutil.rmtree(tree_dist)
            else:
                tree_dist.unlink()
        except OSError as exc:
            logger.warning("Failed to clear %s before symlink: %s", tree_dist, exc)
            return None
    try:
        tree_dist.symlink_to(candidate)
    except OSError as exc:
        logger.warning("Failed to symlink %s -> %s: %s", tree_dist, candidate, exc)
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
    """
    website_dir = proj_path / _DIR_NAME
    if not website_dir.is_dir():
        log("  ⚠️  No website/ directory — skipping frontend build")
        return
    if not shutil.which("npm"):
        log("  ⚠️  npm not found — skipping frontend build")
        return

    log("  🔨 Building frontend (npm)…")
    install_args = (
        ["ci", "--no-audit", "--no-fund"]
        if (website_dir / "package-lock.json").is_file()
        else ["install", "--no-audit", "--no-fund"]
    )
    try:
        r = subprocess.run(
            ["npm", *install_args],
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
            ["npm", "run", "build"],
            cwd=str(website_dir), capture_output=True, timeout=_BUILD_TIMEOUT,
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
    """
    proj_path = Path(proj)
    website_dir = proj_path / _DIR_NAME

    def _warn(msg: str) -> None:
        if push_progress:
            push_progress("warning", msg)

    if not website_dir.is_dir():
        _warn("No website/ directory -- skipping frontend build")
        return
    if not shutil.which("npm"):
        _warn("npm not found -- skipping frontend build")
        return

    install_args = (
        ["ci", "--no-audit", "--no-fund"]
        if (website_dir / "package-lock.json").is_file()
        else ["install", "--no-audit", "--no-fund"]
    )
    npm_i = await asyncio.create_subprocess_exec(
        "npm", *install_args,
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
        "npm", "run", "build",
        cwd=str(website_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
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
