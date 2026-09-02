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
from kiro_crew.node_modules_txn import NodeModulesBackup

logger = logging.getLogger(__name__)

# In-tree frontend directory name (under the repo root). The legacy sibling
# clone directory name is kept only for last-resort dist resolution.
_DIR_NAME = "website"
_SIBLING_DIR_NAME = "KiroCrewWebsite"

# Build timeouts (seconds). npm installs/builds can be slow on cold caches.
_INSTALL_TIMEOUT = 300
_BUILD_TIMEOUT = 300
#: How long to wait for a killed install tree to actually exit before restoring
#: over it. Short by design: the group has already been SIGKILLed, so this only
#: covers reaping, and waiting longer would delay a recovery that is already late.
_REAP_TIMEOUT = 30
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
    # `npm ci` deletes node_modules BEFORE it installs, so a refusal from the
    # registry leaves no tree at all -- and the registry is the one thing needed
    # to rebuild one. Move it aside and put it back unless the install succeeds.
    #
    # The whole transaction runs under ONE holder of the staging lock, install
    # included. That is not about `website/dist` -- it is what makes `begin`'s
    # recovery branch safe. That branch adopts a backup it finds beside the tree,
    # and it cannot tell a CRASHED earlier run's backup (adopt it) from a LIVE
    # peer's (leave it alone); nothing on disk distinguishes them. Serializing the
    # armed interval means a live peer cannot be in it, so the only backup `begin`
    # can ever see is a dead run's. Without that, two updates could each adopt the
    # other's stash and one's commit would delete the tree the other still needed.
    #
    # It must be one holder, not two: the lock is an flock keyed per
    # open-file-description, so re-entering through a second open() in this same
    # process would deadlock against itself (see _staging_lock). Hence the build
    # and stage happen inside here too, via the _locked variant.
    #
    # The cost is real and deliberate: a peer now waits for an install (up to
    # _INSTALL_TIMEOUT) rather than only for a build. Two frontend builds on one
    # checkout were already mutually destructive, so waiting is the correct
    # outcome, not a regression.
    #
    # RESIDUAL: this closes races between Kiro Crew's own Python flows. Dev Fleet's
    # Pull+Build takes this same lock for its build+stage child, but its `npm ci`
    # step runs from a generated stdlib-only script that cannot import kiro_crew
    # and so cannot take it. A Pull+Build install overlapping one of these is
    # therefore still possible; it is tracked separately rather than papered over.
    backup = NodeModulesBackup(website_dir / "node_modules", lambda m: log(f"  ⚠️  {m}"))

    def _reap_tree(proc) -> None:
        """Kill the install's whole process group, then wait for it.

        Both halves matter. The GROUP, because `npm ci` spawns node and any
        lifecycle scripts, and survivors would write into the directory being
        restored. The WAIT, because a killed process is not yet a finished one.
        Every failure is suppressed: the group can exit between the decision to
        kill and the kill itself, and a ProcessLookupError escaping from a
        best-effort reap would turn a recoverable install failure into a crash.
        """
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError, ValueError):
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
            proc.communicate(timeout=_REAP_TIMEOUT)

    proc = None
    try:
        with _staging_lock(proj_path / "src" / "kiro_crew" / "static"):
            if not backup.begin():
                return
            try:
                try:
                    # Popen rather than subprocess.run: run() never exposes the
                    # pid, and without it only the direct child can be signalled
                    # on timeout. `npm ci` spawns node and any lifecycle scripts
                    # the lockfile asks for, and those keep writing into
                    # node_modules after their parent dies -- so a survivor would
                    # race the rollback and land its leftovers in the restored
                    # tree. Its own group, so the whole tree can be signalled.
                    proc = subprocess.Popen(
                        [npm, *install_args],
                        cwd=str(website_dir),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=platform_compat.IS_POSIX,
                        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                    )
                except OSError as exc:
                    backup.rollback()
                    log(f"  ⚠️  Frontend npm install could not start ({exc}) — tree left as it was")
                    return
                try:
                    proc.communicate(timeout=_INSTALL_TIMEOUT)
                except subprocess.TimeoutExpired:
                    # A timeout KILLS npm mid-install, so what it leaves is a
                    # PARTIAL tree -- exactly what the rollback exists for, not a
                    # reason to skip it.
                    _reap_tree(proc)
                    backup.rollback()
                    log("  ⚠️  Frontend npm install timed out — the dependency tree was left as it was")
                    return
                if proc.returncode != 0:
                    backup.rollback()
                    log("  ⚠️  Frontend npm install failed — the dependency tree was left as it was")
                    return
                backup.commit()
            except BaseException:
                # Ctrl-C is the case this exists for: KeyboardInterrupt is a
                # BaseException, so none of the handlers above see it, and without
                # this the tree would stay stashed under its backup name while the
                # path the rest of the app reads is simply missing. Covers the
                # whole armed interval, so no future edit inside it can
                # reintroduce the gap. rollback() is a no-op once commit() or an
                # earlier rollback disarmed it.
                #
                # Reap FIRST, and note WHY that is not optional here: the install
                # runs in its own session (so its whole tree can be signalled on
                # timeout), which also means a terminal Ctrl-C does NOT reach it --
                # SIGINT goes to the foreground process group, and npm is no longer
                # in it. So npm survives the interrupt and would keep writing into
                # the directory being restored.
                _reap_tree(proc)
                backup.rollback()
                raise
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
    # `npm ci` deletes node_modules BEFORE it installs. This is the UNATTENDED
    # path: the gateway's auto-apply reaches it at boot with no operator, and it
    # never retries, because the next boot sees the commit already applied. So a
    # registry refusal here destroyed the tree until a human happened to notice.
    #
    # The transaction's removals and renames are BLOCKING filesystem work on a
    # tree of tens of thousands of files, so they run in a worker thread for the
    # same reason the build+stage below does: on the event loop they would stall
    # the gateway's heartbeat and every in-flight request. And it collects its
    # messages instead of warning directly, because `_warn` reaches
    # `push_progress`, which belongs to the LOOP thread -- they are replayed here.
    txn_log: list[str] = []
    backup = NodeModulesBackup(website_dir / "node_modules", txn_log.append)

    def _drain() -> None:
        while txn_log:
            _warn(txn_log.pop(0))

    # Submitted through the executor DIRECTLY rather than loop.run_in_executor, so
    # the concurrent future is in hand: a cancelled `await` does NOT cancel work
    # that has already started in the thread, and the lock must not be released
    # while such a step is still renaming or removing the tree -- that would admit
    # a peer mid-mutation. Tracked here, drained in the finally.
    inflight: list = []

    async def _offload(step):
        future = subprocess_executor().submit(step)
        inflight.append(future)
        try:
            result = await asyncio.wrap_future(future)
        finally:
            _drain()
        # Reached only when the step actually completed. A cancelled await skips
        # this, so the future stays tracked and the finally waits for it.
        inflight.remove(future)
        return result

    async def _kill_and_wait(proc) -> None:
        """Kill npm AND its descendants, then wait, before anything clears the tree.

        `npm ci` is not a leaf: it spawns node and any lifecycle scripts the
        lockfile asks for, and those keep writing into `node_modules` after their
        parent dies. Killing only the direct child therefore left writers racing
        the rollback. `kill_and_reap` signals the whole group, which is why the
        spawn below opens a session of its own -- a child sharing our group has no
        tree to signal and the group kill is skipped for it.

        The wait is the load-bearing half: a killed child is not a finished one.
        """
        if proc is None:
            return
        if proc.returncode is not None:
            # Already exited: nothing to signal, and nothing to wait for. This
            # matters because the interruption handlers also cover the build and
            # stage, which run AFTER the install has finished -- reaping there
            # would signal a pid that is gone (or, worse, reused).
            return
        with contextlib.suppress(asyncio.CancelledError, ProcessLookupError, OSError):
            await platform_compat.kill_and_reap(proc)

    npm_i = None
    # ONE holder of the staging lock spans this whole transaction, install
    # included. That is what makes `begin`'s recovery branch safe: it adopts a
    # backup it finds beside the tree and cannot tell a CRASHED earlier run's from
    # a LIVE peer's, because nothing on disk distinguishes them. Serializing the
    # armed interval means a live peer cannot be inside it, so the only backup
    # `begin` can see is a dead run's. See build_frontend_sync for the same
    # reasoning, its cost, and the Dev Fleet residual.
    #
    # It is entered and exited through the executor because taking a blocking
    # flock on the event loop would freeze the gateway for the length of someone
    # else's install -- and it must be ONE holder, since the lock is keyed per
    # open-file-description and re-entering in this process would deadlock.
    lock = contextlib.ExitStack()
    static_parent = proj_path / "src" / "kiro_crew" / "static"
    messages: list[str] = []
    staged = False
    try:
        await _offload(lambda: lock.enter_context(_staging_lock(static_parent)))
    except OSError as exc:
        _warn(f"Could not acquire the static/dist staging lock: {exc}")
        return
    try:
        # begin() is INSIDE the protected interval: it arms the transaction in a
        # worker thread, so a cancellation delivered just after the rename but
        # before the body would otherwise leave the tree stashed with nothing to
        # put it back.
        if not await _offload(backup.begin):
            return
        try:
            npm_i = await asyncio.create_subprocess_exec(
                npm, *install_args,
                cwd=str(website_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                # Its own group, so the whole install tree can be signalled --
                # see _kill_and_wait. No-op on the platform that lacks each half.
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
        except OSError as exc:
            await _offload(backup.rollback)
            _warn(f"Frontend npm install could not start ({exc}) -- tree left as it was")
            return
        try:
            await asyncio.wait_for(npm_i.wait(), timeout=_INSTALL_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_and_wait(npm_i)
            # Killed mid-install, so what is on disk is PARTIAL. Restore before
            # reporting, so the message is true by the time anyone reads it.
            await _offload(backup.rollback)
            _warn("Frontend npm install timed out -- the dependency tree was left as it was")
            return
        if npm_i.returncode != 0:
            await _offload(backup.rollback)
            _warn("Frontend npm install failed -- the dependency tree was left as it was")
            return
        await _offload(backup.commit)

        # Still inside the SAME holder, so this calls the _locked variant --
        # re-entering _staging_lock here would deadlock against ourselves. Vite
        # empties website/dist, so a peer staging concurrently would copy a
        # partially written tree.
        def _build_and_stage() -> bool:
            # Collect rather than calling _warn: this runs on a worker thread, and
            # _warn reaches push_progress, which belongs to the loop thread.
            #
            # OSError is caught HERE rather than left to the interruption handlers
            # below. The build spawns its own npm, so a missing binary raises
            # FileNotFoundError -- an OSError -- and letting that propagate would
            # turn a reported build failure into an exception escaping this helper,
            # which on the unattended path means it escapes into the gateway's
            # auto-apply. Reporting it keeps the caller's contract: this function
            # warns and returns, it does not raise for a failed build.
            try:
                return _npm_build_and_stage_locked(
                    website_dir, proj_path, npm, messages.append
                )
            except OSError as exc:
                messages.append(f"Frontend build could not run: {exc}")
                return False

        # Through _offload, NOT raw run_in_executor: that is what puts the future
        # in `inflight` so the `finally` waits for it before releasing the lock.
        # Otherwise a cancellation here releases the flock while this thread is
        # still running `npm run build` (which rewrites website/dist) and staging
        # it, and a peer would publish a bundle vite is mid-rewrite -- the mixed
        # bundle the lock exists to prevent. Before this PR the lock was taken
        # INSIDE the worker, so a cancelled await could not release it early.
        staged = await _offload(_build_and_stage)
    except asyncio.CancelledError:
        # Gateway shutdown during the install. Left alone this strands the tree:
        # ours stays stashed while npm keeps writing a fresh one, and if npm gets
        # far enough BOTH paths exist -- which the next run can only read as
        # ambiguous, refusing every build until someone clears one by hand.
        #
        # Reap FIRST. The child outlives its cancelled parent, so clearing the
        # directory it is writing would race it.
        await _kill_and_wait(npm_i)
        # Then roll back SYNCHRONOUSLY rather than through the executor. This is
        # MAINTAINER-ADJUDICATED, not a preference: `executors.py` registers an
        # atexit shutdown that calls `shutdown(wait=False, cancel_futures=True)`,
        # so at interpreter exit -- which is exactly when this path runs -- queued
        # executor work is CANCELLED and running work is not waited for. Offloading
        # here would turn a guaranteed recovery into a probabilistic one in the one
        # scenario the transaction exists for. Shielding the await does not help:
        # it protects the await, not the queued future. A brief block during a
        # shutdown that is already ending is the cheaper side of that trade.
        backup.rollback()
        _drain()
        raise
    except BaseException:
        # Any other interruption across the armed interval -- SystemExit, or a
        # cancellation raised somewhere an await is not expected. Unlike the
        # cancelled case above this task is still live, so the cleanup can go
        # through the executor, and the re-raise waits for it to finish.
        await _kill_and_wait(npm_i)
        await _offload(backup.rollback)
        raise
    finally:
        # Drain before releasing. A cancelled await leaves its executor step
        # RUNNING, so the lock would otherwise be released while a thread is still
        # renaming or removing the tree, admitting a peer into a half-applied
        # transaction. Waited on synchronously, for the same adjudicated reason the
        # cancellation rollback is synchronous: this runs during shutdown, where a
        # further await is not guaranteed to resume and the executor is being torn
        # down with cancel_futures=True.
        for future in inflight:
            with contextlib.suppress(Exception):
                future.result(timeout=_REAP_TIMEOUT)
        _drain()
        # Releasing is an flock release and a file close -- microseconds, unlike
        # the tree work -- so doing it inline is safe even on the cancelled path.
        with contextlib.suppress(Exception):
            lock.close()

    for message in messages:
        # Surface the specific cause (build timeout / build failure / staging
        # refusal / lock failure) rather than one generic line: without it the
        # update flow reports success and restarts while the dashboard still
        # serves the PREVIOUS bundle, so a user sees no reason it did not apply.
        _warn(message.strip().lstrip("⚠️ ").strip() or "Frontend build/staging failed")
    if not staged and not messages:
        _warn("Frontend build/staging failed -- dashboard may be stale")
