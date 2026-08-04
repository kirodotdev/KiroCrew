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
import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Optional

from kiro_crew import platform_compat
from kiro_crew.executors import subprocess_executor

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
# Seconds to wait for a SIGKILLed build to be reaped before giving up. SIGKILL is
# not catchable, so this only covers the kernel tearing the tree down; there are
# no pipes to drain because the build's output goes to DEVNULL.
_BUILD_KILL_GRACE = 10

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


def _incomplete_bundle_reason(tree: Path) -> str:
    """Why ``tree`` is not a complete built frontend, or ``""`` if it is.

    ``index.html`` alone does not prove completeness: Rollup writes the entry
    document and the hashed chunks it references separately, so a tree copied
    out from under a concurrent build can carry an index whose chunks are
    missing. Publishing that yields a shell whose every chunk 404s.

    Only ``/assets/`` references are resolved — that is where Vite emits the
    content-hashed chunks, so it is the completeness signal. The index also
    references paths the GATEWAY serves by route rather than from the bundle
    (``/manifest.js``), and those must not be mistaken for missing files.
    """
    index = tree / "index.html"
    if not index.is_file():
        return "no index.html"
    try:
        html = index.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"index.html is unreadable ({exc})"
    refs = re.findall(r'(?:src|href)="(/assets/[^"?#]+\.(?:js|css))', html)
    missing = [ref for ref in refs if not (tree / ref.lstrip("/")).is_file()]
    if missing:
        return f"{len(missing)} referenced asset(s) missing, e.g. {missing[0]}"
    return ""


@contextlib.contextmanager
def _staging_lock(static_parent: Path) -> Iterator[None]:
    """Hold the cross-process staging lock for ``static/dist``.

    Serializes every build or stage of the frontend initiated by Kiro Crew: Dev
    Fleet's Pull+Build and the dashboard update flow can run at once, and BOTH
    the ``npm run build`` (which empties ``website/dist``) and the copy/swap must
    be inside one holder. Covering only the copy still lets a peer's build rewrite
    the tree mid-read, and a bundle's lazy chunks are not reachable from
    ``index.html``, so no post-hoc inspection can detect that reliably.

    Raises ``OSError`` if the lock cannot be taken. Callers holding this MUST
    call ``_stage_dist_locked`` rather than ``_stage_dist``: the lock is an
    flock keyed per open-file-description, so re-entering through a second
    ``open()`` in the same process would deadlock against itself.
    """
    static_parent.mkdir(parents=True, exist_ok=True)
    lock_path = static_parent / ".dist.staging.lock"
    with open(lock_path, "a+") as lock_fh:
        # required=True: Windows msvcrt acquisition failures are otherwise
        # swallowed, and running without exclusion is the very outage this
        # lock exists to prevent.
        with platform_compat.file_lock(
            lock_fh.fileno(), exclusive=True, required=True
        ):
            yield


def _npm_build_and_stage_locked(
    website_dir: Path,
    proj_path: Path,
    npm: str,
    log: Callable[[str], None],
) -> bool:
    """Run ``npm run build`` then stage it. Caller holds the staging lock.

    The build is spawned in its own process group and the whole tree is reaped
    on timeout. ``npm run build`` is ``tsc -b && vite build``, so killing only
    npm would leave vite writing ``website/dist`` after this function returns
    and the lock releases — a surviving writer makes the lock's exclusion
    meaningless, since a peer could then stage a tree vite is still rewriting.
    """
    proc = subprocess.Popen(
        [npm, "run", "build"],
        env=_edition_build_env(),
        cwd=str(website_dir),
        # DEVNULL, not PIPE: nothing reads the build's output, and pipes would
        # make the post-kill drain block until every grandchild closes its
        # inherited write handle — inside the lock holder, which would then
        # never release it.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        proc.wait(timeout=_BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Enumerate BEFORE killing: the kill reparents survivors to init and
        # erases the PPID links that identify them. The group kill alone misses
        # a descendant that started its own session, and such an escapee keeps
        # rewriting website/dist after this holder releases the staging lock —
        # the mixed-bundle publication this lock exists to prevent.
        descendants = platform_compat.process_descendants(proc.pid)
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except (ProcessLookupError, OSError, ValueError) as exc:
            log(f"  ⚠️  Could not reap the timed-out frontend build: {exc}")
        for child in descendants:
            try:
                platform_compat.kill_process_tree(child, platform_compat.SIGKILL)
            except (ProcessLookupError, OSError, ValueError):
                # Already reaped by the group kill, or no longer signalable.
                continue
        # Reap the direct child so it is not left a zombie. Bounded, so a
        # survivor cannot hold the staging lock open indefinitely.
        try:
            proc.wait(timeout=_BUILD_KILL_GRACE)
        except subprocess.TimeoutExpired:
            log("  ⚠️  Frontend build did not die after SIGKILL")
        log("  ⚠️  Frontend build timed out — dashboard may be stale")
        return False
    if proc.returncode != 0:
        log("  ⚠️  Frontend build failed — dashboard may be stale")
        return False
    static_dist = proj_path / "src" / "kiro_crew" / "static" / "dist"
    return _stage_dist_locked(website_dir / "dist", static_dist, log)


def build_and_stage(
    proj_path: "str | Path | None" = None,
    npm: str | None = None,
    log: Callable[[str], None] = print,
) -> bool:
    """Build this install's frontend and stage it, both under one lock.

    The entry point for callers that build an install they do NOT run
    in-process — notably Dev Fleet's Pull+Build. Holding the lock across the
    build is what makes the result safe to publish: ``npm run build`` empties
    ``website/dist``, so a peer flow staging concurrently would otherwise copy
    a partially written tree.

    ``proj_path`` accepts a string because the callers that need it are
    out-of-process and pass it through ``argv``. ``npm`` names the executable to
    run, so a caller that resolved a trusted path passes it rather than having it
    re-resolved here. Returns ``True`` when ``static/dist`` holds the newly built
    bundle.
    """
    root = (
        Path(proj_path)
        if proj_path is not None
        else Path(__file__).resolve().parents[2]
    )
    website_dir = root / _DIR_NAME
    if not website_dir.is_dir():
        log(f"  ⚠️  No {_DIR_NAME}/ directory at {root} — nothing to build")
        return False
    npm_bin = npm or shutil.which("npm")
    if not npm_bin:
        log("  ⚠️  npm not found — cannot build the frontend")
        return False
    try:
        with _staging_lock(root / "src" / "kiro_crew" / "static"):
            return _npm_build_and_stage_locked(website_dir, root, npm_bin, log)
    except OSError as exc:
        log(f"  ⚠️  Could not acquire the static/dist staging lock: {exc}")
        return False


def _discard_path(path: Path) -> None:
    """Best-effort remove a file, symlink or directory.

    A staged-aside entry can be any of the three — ``static/dist`` is a symlink
    on a source install and a real tree once staged — and ``shutil.rmtree``
    refuses a symlink even though ``is_dir()`` follows it and returns True.
    """
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _stage_dist(
    built_dist: Path,
    proj_path: Path,
    log: Callable[[str], None] = print,
) -> bool:
    """Copy a freshly built dist into ``static/dist``.

    A copy (rather than a symlink) is used so the served bundle is a
    self-contained snapshot independent of later ``website/`` rebuilds —
    important for packaged/installed layouts. That independence is load-bearing
    for a *running* gateway too: aiohttp resolves a static route's directory once
    at registration, so a gateway started while ``static/dist`` was a symlink
    (see :func:`ensure_dev_dist_symlink`) is pinned to ``website/dist`` for its
    whole life and 404s while Vite rewrites that directory. Staging makes the
    NEXT start serve an independent tree.

    The copy lands in a temporary sibling and is swapped in with a single
    ``os.replace``, so a concurrently-serving gateway never sees a half-copied
    tree. The live tree is moved aside rather than deleted, and restored if the
    swap fails, so a failed stage never leaves the dashboard with no assets:
    either the new bundle is published or the previous one is still there.

    Returns ``True`` when ``static/dist`` now holds the new bundle. Callers that
    treat staging as best-effort can keep ignoring the result — the failure is
    still logged — but a caller whose own success depends on staging (Dev Fleet's
    Pull+Build) must check it, because a preserved older bundle is no longer
    evidence that anything was staged.
    """
    static_dist = proj_path / "src" / "kiro_crew" / "static" / "dist"
    # Staging alone takes the lock; callers that also BUILD must hold it across
    # both (see build_and_stage), since the build rewrites the tree this copies.
    try:
        with _staging_lock(static_dist.parent):
            return _stage_dist_locked(built_dist, static_dist, log)
    except OSError as exc:
        log(f"  ⚠️  Could not acquire the static/dist staging lock: {exc}")
        return False


def _stage_dist_locked(
    built_dist: Path,
    static_dist: Path,
    log: Callable[[str], None],
) -> bool:
    """Sweep, copy and swap. Caller holds the staging lock."""
    # Under the lock every staging tree present is abandoned residue from a run
    # that was killed mid-copy; left alone each one is ~30 MB of untracked
    # residue that makes the checkout read as permanently dirty, which
    # fail-closes Dev Fleet's prune.
    for stale in static_dist.parent.glob(".dist.staging.*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    # Validated after the sweep, so refusing an unusable source still clears
    # residue rather than leaving the checkout dirty.
    if not built_dist.is_dir():
        log(f"  ⚠️  Built dist not found at {built_dist} — dashboard may be stale")
        return False
    reason = _incomplete_bundle_reason(built_dist)
    if reason:
        # An out-of-band build — one that takes no staging lock, such as pod
        # provisioning — can be observed mid-rebuild, and publishing that would
        # replace a good bundle with a broken one.
        log(f"  ⚠️  {built_dist} is not a complete build ({reason}) — not staging")
        return False
    tmp_dist: Path | None = None
    try:
        # Same parent as the destination so the swap is a rename within one
        # filesystem; a cross-device staging dir would make os.replace fail.
        tmp_dist = Path(
            tempfile.mkdtemp(prefix=".dist.staging.", dir=static_dist.parent)
        )
        # mkdtemp already created it, but copytree needs to create the target.
        tmp_dist.rmdir()
        shutil.copytree(built_dist, tmp_dist)
    except OSError as exc:
        # tmp_dist stays None when mkdtemp itself fails (ENOSPC, quota), so the
        # cleanup is conditional — an unconditional rmtree would raise
        # UnboundLocalError and mask the real error.
        log(f"  ⚠️  Could not copy static/dist: {exc}")
        if tmp_dist is not None:
            shutil.rmtree(tmp_dist, ignore_errors=True)
        return False
    assert tmp_dist is not None  # bound above or we returned
    reason = _incomplete_bundle_reason(tmp_dist)
    if reason:
        # The source passed its pre-copy check but changed while being read — a
        # peer flow's `npm run build` rewriting website/dist mid-copy. Swapping
        # this in would replace a valid served bundle with a partial one.
        log(f"  ⚠️  Staged copy is incomplete ({reason}) — not publishing")
        shutil.rmtree(tmp_dist, ignore_errors=True)
        return False
    backup: Path | None = None
    try:
        # Move whatever is in place aside rather than deleting it — a symlink
        # (the normal source install) just as much as a staged tree — so a
        # failed publication can put it back. Deleting first means a replace
        # error publishes nothing and the dashboard serves no assets at all.
        # is_symlink() is checked first so a BROKEN symlink is still moved.
        if static_dist.is_symlink() or static_dist.exists():
            backup = static_dist.parent / f".dist.previous.{os.getpid()}"
            _discard_path(backup)
            os.replace(static_dist, backup)
        os.replace(tmp_dist, static_dist)
    except OSError as exc:
        log(f"  ⚠️  Could not stage static/dist: {exc}")
        published = static_dist.is_symlink() or static_dist.exists()
        if backup is not None and not published:
            try:
                os.replace(backup, static_dist)
            except OSError as restore_exc:
                # Leave the backup on disk: it is the only remaining copy of
                # what was being served, so it must not be swept away.
                log(
                    "  ⚠️  Could not restore the previous static/dist "
                    f"({restore_exc}); it is preserved at {backup}"
                )
            else:
                backup = None
        shutil.rmtree(tmp_dist, ignore_errors=True)
        return False
    # Published. The superseded entry, and any older one a failed restore
    # preserved, are safe to drop now that a good bundle is in place.
    if backup is not None:
        _discard_path(backup)
    for old in static_dist.parent.glob(".dist.previous.*"):
        _discard_path(old)
    log(f"  📦 Staged static/dist ← {built_dist}")
    return True


def edition_configured() -> bool:
    """True when an edition composition root is configured for this process.

    A rebuild that cannot pass the edition seam through to vite can only produce
    a STOCK SPA (see :func:`_edition_build_env`), so a caller that STAGES build
    output must skip rather than replace an edition dashboard with upstream's.
    Distinct from :func:`edition_sources_missing`, which answers whether the
    sources are present; this answers whether an edition is in play at all.
    """
    return bool(os.environ.get(_EDITION_DIR_ENV))


def stage_built_dist(
    proj_path: "str | Path",
    log: Callable[[str], None] = print,
) -> None:
    """Stage an ALREADY-built ``website/dist`` into the served ``static/dist``.

    The public seam for callers that run the npm build themselves and only need
    the staging half — Dev Fleet's Pull+Build, which drives each build step as
    its own audited subprocess and so cannot call
    :func:`build_frontend_sync`'s all-in-one path.

    Without this step a Pull+Build leaves the new bundle in ``website/dist``
    while the gateway keeps serving the old ``static/dist``. On a source-tree
    gateway start that goes unnoticed because :func:`ensure_dev_dist_symlink`
    has already linked the two; with a packaged install there is no link, so the
    rebuild silently never takes effect.

    Raises ``RuntimeError`` when staging did not happen.
    :func:`_stage_dist` logs and returns ``False`` on failure because its other
    callers treat staging as best-effort; here it is a SYNC STEP whose exit
    status decides whether Pull+Build reports success. Note that a surviving
    older bundle is NOT evidence of success -- `_stage_dist` now preserves it on
    failure -- so this checks the returned flag rather than merely asserting that
    something is present at the destination.

    The caller is responsible for not invoking this after a build that could not
    recompose an edition — see :func:`edition_configured`.
    """
    proj = Path(proj_path)
    built = proj / "website" / "dist"
    if not _stage_dist(built, proj, log):
        raise RuntimeError(
            f"dist staging failed; the dashboard still serves the previous "
            f"bundle (built dist: {built})"
        )


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

    # npm install does not touch website/dist, so only the build+stage pair
    # needs the lock.
    try:
        with _staging_lock(proj_path / "src" / "kiro_crew" / "static"):
            _npm_build_and_stage_locked(website_dir, proj_path, npm, log)
    except OSError as exc:
        log(f"  ⚠️  Could not acquire the static/dist staging lock: {exc}")


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

    # The build and the stage run under ONE lock holder, in a worker thread.
    # Vite empties website/dist, so a peer flow staging concurrently would copy
    # a partially written tree; and acquiring a blocking flock on the event loop
    # would freeze the gateway for the length of someone else's build.
    messages: list[str] = []

    def _locked_build_and_stage() -> bool:
        # Collect rather than calling _warn: this runs on a worker thread, and
        # _warn reaches push_progress, which belongs to the loop thread.
        try:
            with _staging_lock(proj_path / "src" / "kiro_crew" / "static"):
                return _npm_build_and_stage_locked(
                    website_dir, proj_path, npm, messages.append
                )
        except OSError as exc:
            messages.append(f"Could not acquire the static/dist staging lock: {exc}")
            return False

    staged = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), _locked_build_and_stage
    )
    for message in messages:
        # Surface the specific cause (build timeout / build failure / staging
        # refusal / lock failure) rather than one generic line: without it the
        # update flow reports success and restarts while the dashboard still
        # serves the PREVIOUS bundle, so a user sees no reason it did not apply.
        _warn(message.strip().lstrip("⚠️ ").strip() or "Frontend build/staging failed")
    if not staged and not messages:
        _warn("Frontend build/staging failed -- dashboard may be stale")
