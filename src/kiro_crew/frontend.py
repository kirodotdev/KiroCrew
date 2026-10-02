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

import argparse
import asyncio
import contextlib
import errno
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterator, Optional

from kiro_crew import platform_compat
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.executors import subprocess_executor
from kiro_crew.node_modules_txn import NodeModulesBackup

logger = logging.getLogger(__name__)

# In-tree frontend directory name (under the repo root). The legacy sibling
# clone directory name is kept only for last-resort dist resolution.
_DIR_NAME = "website"
_SIBLING_DIR_NAME = "KiroCrewWebsite"

# Build timeouts (seconds). These are WEDGE backstops, not schedules: whatever is
# still running when one expires is SIGKILLed, so the build budget has to clear
# the slowest HEALTHY build while still FIRING BEFORE the caller's own deadline --
# a budget that never fires cannot report anything.
#
# 300s does not cover the build. `npm run build` is `tsc -p tsconfig.app.json`
# followed by a production bundle; on one developer machine it took 75-98s as a
# repeat build but 328s and 420s on the first build after `npm ci` -- and it is
# that slow case the budget has to clear, because the caller which hits this most,
# Dev Fleet's Pull+Build, always builds immediately after `npm ci`. The type-check
# is the bulk of it. It keeps an incremental cache beside tsconfig.app.json that
# survives `npm ci`, so a repeat build re-checks only what changed -- but a cold
# clone has no cache, and a Pull+Build that moved the lockfile re-hashes the new
# node_modules, so the budget is sized for the uncached case.
#
# The CEILING is that same caller: dev_fleet's stream watchdog kills the whole
# sync run at ``runtime._RUN_DEADLINE_S`` (1800s), counted from fetch -- before
# preflight, merge, pip and `npm ci` have even reached the build. A build budget
# at or near 1800s therefore never expires there; the watchdog kills the tree
# first and the warning below is never emitted, which is the silent-stale-bundle
# outage this change exists to end. So the build claims at most HALF that
# deadline, leaving the other half for everything the sync does before it.
#
# Residual, deliberately not closed here: if the steps before the build consume
# more than the build budget, the watchdog still pre-empts it and reports its own
# `[timeout] ... deadline` line instead of the specific one below. Closing that
# needs the REMAINING deadline threaded from dev_fleet into the build child, which
# is a change to that app's run supervisor rather than to this module.
#
# _INSTALL_TIMEOUT is left at its original value: the installs measured here ran
# 11s (warm cache) and 136s (full), which it already clears, and dev_fleet's own
# `npm ci` is a separate raw step this does not bound at all.
_INSTALL_TIMEOUT = 300
_BUILD_TIMEOUT = 900
#: Allowance for the copy/swap that follows the install and build inside the same
#: staging-lock holder. Generous relative to a tree copy so a loaded host does
#: not turn a working holder into a refused contender.
_STAGING_SWAP_ALLOWANCE = 120
#: How long a contender waits for ``_staging_lock``. The holder legitimately
#: spans an ``npm ci``, an ``npm run build`` and the copy/swap, so the wait must
#: outlast their sum: ``platform_compat``'s default ceiling is sized for a
#: sub-second critical section and would refuse a contender while the holder is
#: still working rather than because it is stuck. Derived from the bounds it must
#: cover so the two cannot drift apart.
_STAGING_LOCK_TIMEOUT = float(
    _INSTALL_TIMEOUT + _BUILD_TIMEOUT + _STAGING_SWAP_ALLOWANCE
)
#: How long to wait for a killed install tree to actually exit before restoring
#: over it. Short by design: the group has already been SIGKILLed, so this only
#: covers reaping, and waiting longer would delay a recovery that is already late.
_REAP_TIMEOUT = 30
# Seconds to wait for a SIGKILLed build to be reaped before giving up. SIGKILL is
# not catchable, so this only covers the kernel tearing the tree down; there are
# no pipes to drain because the build's output goes to DEVNULL.
_BUILD_KILL_GRACE = 10
#: How long ``python -m kiro_crew.frontend stage`` waits for the staging lock.
#: A holder is a Kiro Crew build that publishes its own bundle, so a stager
#: run by hand reports that plainly rather than queueing behind the build.
_CLI_STAGE_LOCK_TIMEOUT = 30.0
#: How long gateway start waits for the staging lock before an edition serves
#: the build it found through the link instead of a private copy. Short: the
#: whole start waits on it, and a holder is a build that publishes its own
#: bundle anyway.
_GATEWAY_START_LOCK_TIMEOUT = 5.0
#: Prefix of every entry staging keeps beside ``static/dist``: the immutable
#: copies ``static/dist`` links to, the link being swapped in, and the lock.
_STAGED_PREFIX = ".dist."
_STAGING_LOCK_NAME = ".dist.staging.lock"
#: Written into every staged copy: a checkout at a revision whose own
#: ``.gitignore`` predates the copies' names still reads it as ignored, so a
#: Dev Fleet stage of an older target never leaves that worktree dirty.
_STAGED_COPY_IGNORE = (".gitignore", "*\n")

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
    2. Existing link → kept while its target holds ``index.html``. A link to
       this checkout's ``website/dist`` is kept too while that target is
       dangling or index-less, except under an edition: the build is
       mid-publish or not built yet, and since the dashboard resolves
       ``static/dist`` on every request, every build route serves it the moment
       it lands. App window entries are the exception: they are enumerated when
       the gateway starts, so a window first built after start needs a restart.
       Any other dangling or index-less link is replaced.
    3. Missing → resolve the in-tree ``website/dist`` (or a sibling
       ``KiroCrewWebsite`` checkout as a last resort) and symlink to it. When
       nothing is built yet, a stock checkout still links its ``website/dist``
       (the link Case 2 keeps), so the first build is served the moment it
       lands; ``None`` is returned all the same, as nothing is served yet.

    Symlink over copy: no source-tree churn, ``.gitignore`` already excludes
    ``static/dist/``, and a fresh ``website`` rebuild propagates to the gateway
    with no extra step, a running one included: every Vite build publishes into
    ``website/dist`` atomically (``website/scripts/publish-dist.mjs``).

    Under an edition (:func:`edition_configured`) no link is made: the bundle is
    staged as a private copy, so a later stock build in the same checkout cannot
    replace the edition dashboard. If that stage fails (the staging lock is still
    held after :data:`_GATEWAY_START_LOCK_TIMEOUT`, or the copy fails), the
    gateway serves the build it found through the link and logs a warning rather
    than starting with no dashboard. Gateway start calls this off the event loop,
    so that lock wait is a real wait.

    Returns the resolved dist path on success, ``None`` if nothing could be
    found (caller should warn; the gateway then serves the "not built"
    guidance page — there is no legacy dashboard fallback).
    """
    kiro_crew_pkg_dir = Path(__file__).resolve().parent
    tree_dist = kiro_crew_pkg_dir / "static" / "dist"
    repo_root = _repo_root(kiro_crew_pkg_dir)

    # A prior run may have created a symlink (POSIX) OR a directory junction
    # (non-admin Windows); both are "links" here and neither is a real dir.
    tree_dist_is_link = platform_compat.is_link_or_junction(tree_dist)

    # Case 1: real directory already populated (packaged install / a prior
    # local build landing in the source tree / user ran kirocrew init --ui).
    if tree_dist.is_dir() and not tree_dist_is_link:
        if _has_index(tree_dist):
            return tree_dist
        # Empty real dir — fall through and try to resolve something usable.

    # Case 2: existing link — re-use it while its target holds a dist.
    source: Optional[Path] = None
    if tree_dist_is_link:
        target = _live_link_target(tree_dist)
        if target is not None and _has_index(target):
            # An edition's own staged copy is already private.
            if not edition_configured() or _is_staged_tree(target, tree_dist):
                return target
            source = target
        elif not edition_configured() and _links_to_website_dist(tree_dist, repo_root):
            return None
        else:
            try:
                platform_compat.unlink_link_or_junction(tree_dist)
            except OSError as exc:
                logger.warning("Failed to remove stale dist link %s: %s", tree_dist, exc)
                return None

    # Case 3: no usable dist in place — probe and link.
    source = source or _resolve_website_dist(kiro_crew_pkg_dir)
    unbuilt = source is None
    if source is None:
        if edition_configured() or not (repo_root / _DIR_NAME).is_dir():
            return None
        source = repo_root / _DIR_NAME / "dist"
    if edition_configured():
        if _stage_dist(source, repo_root, lock_timeout=_GATEWAY_START_LOCK_TIMEOUT):
            return tree_dist
        logger.warning("Could not stage the edition bundle; serving %s through the link", source)
        if _live_link_target(tree_dist) is not None:
            return source

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
        platform_compat.symlink_or_junction(str(source), str(tree_dist))
    except OSError as exc:
        logger.warning("Failed to link %s -> %s: %s", tree_dist, source, exc)
        return None
    logger.info("Linked frontend dist: %s -> %s", tree_dist, source)
    return None if unbuilt else source


def _links_to_website_dist(link: Path, repo_root: Path) -> bool:
    """Whether ``link`` names this checkout's ``website/dist``, built or not."""
    try:
        return os.path.realpath(link) == os.path.realpath(repo_root / _DIR_NAME / "dist")
    except (OSError, ValueError):
        return False


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
    try:
        html = (tree / "index.html").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "no index.html"
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
        return f"index.html is unreadable ({code})"
    refs = re.findall(_ASSET_REF, html)
    missing = [ref for ref in refs if not (tree / ref.lstrip("/")).is_file()]
    if missing:
        return f"{len(missing)} referenced asset(s) missing, e.g. {missing[0]}"
    return ""


#: Every ``/assets/*.js|css`` an index.html references, ignoring a query or hash.
#: Byte-identical to ``ASSET_REF_PATTERN`` in website/scripts/publish-dist.mjs,
#: pinned by test_frontend_dist_resolve.py, so both gates accept the same trees.
_ASSET_REF = r'(?:src|href)="(/assets/[^"?#]+\.(?:js|css))(?:[?#][^"]*)?"'


def _live_link_target(path: Path) -> Optional[Path]:
    """Where the link at ``path`` resolves, or ``None`` if it is no live link.

    ``None`` for a real directory, a dangling link and a looping one; a loop
    raises ``RuntimeError`` from ``resolve`` before Python 3.13.
    """
    if not platform_compat.is_link_or_junction(path):
        return None
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _has_index(tree: Path) -> bool:
    """Whether ``tree`` holds an index.html; an unreadable tree is "no", not a crash."""
    return os.path.isfile(tree / "index.html")


def _same_dir(a: Path, b: Path) -> bool:
    """Whether two paths name one directory, whatever case or spelling reaches it."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _publishes_atomically(built_dist: Path) -> bool:
    """Whether the checkout that built ``built_dist`` publishes every build by rename.

    A Dev Fleet Pull+Build stages older target revisions too, whose Vite build
    writes ``website/dist`` in place; those must not be served through a link.
    """
    return (built_dist.parent / "scripts" / "publish-dist.mjs").is_file()


def _links_to(link: Path, tree: Path) -> bool:
    """Whether ``link`` is a live link to ``tree``."""
    target = _live_link_target(link)
    return target is not None and _same_dir(target, tree)


def _serves_through_dev_link(built_dist: Path, static_dist: Path) -> bool:
    """Whether ``static/dist`` is the dev link to a stock build that publishes atomically.

    An edition bundle is always a private copy, and a build that writes
    ``website/dist`` in place is served from a copy, so neither keeps the link.
    Correctness does not rest on this: the dashboard resolves ``static/dist`` on
    every request, so re-pointing it is safe whichever way this answers.
    """
    if edition_configured() or not _publishes_atomically(built_dist):
        return False
    return _links_to(static_dist, built_dist)


def _print_safe(message: str) -> None:
    """Print a progress line that can never raise.

    A stage that already re-pointed ``static/dist`` must not then fail on its own
    log line: the emoji on a latin-1 or cp1252 stdout, no stdout at all
    (``pythonw``), or a pipe whose reader has gone.
    """
    stream = sys.stdout
    if stream is None:
        return
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        stream.write(message.encode(encoding, errors="replace").decode(encoding) + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass


@contextlib.contextmanager
def _staging_lock(static_parent: Path, timeout: Optional[float] = None) -> Iterator[None]:
    """Hold the cross-process staging lock for ``static/dist``.

    Serializes every build or stage of the frontend initiated by Kiro Crew: Dev
    Fleet's Pull+Build and the dashboard update flow can run at once, and BOTH
    the ``npm run build`` (which swaps a new tree into ``website/dist``) and the copy/swap must
    be inside one holder. Covering only the copy still lets a peer's build rewrite
    the tree mid-read, and a bundle's lazy chunks are not reachable from
    ``index.html``, so no post-hoc inspection can detect that reliably.

    Raises ``OSError`` if the lock cannot be taken. Callers holding this MUST
    call ``_stage_dist_locked`` rather than ``_stage_dist``: the lock is an
    flock keyed per open-file-description, so re-entering through a second
    ``open()`` in the same process would deadlock against itself.
    """
    static_parent.mkdir(parents=True, exist_ok=True)
    lock_path = static_parent / _STAGING_LOCK_NAME
    with open(lock_path, "a+") as lock_fh:
        # required=True: Windows msvcrt acquisition failures are otherwise
        # swallowed, and running without exclusion is the very outage this
        # lock exists to prevent.
        # timeout: this holder runs an install and a build, far past the default
        # ceiling, so a contender must wait for the work rather than be refused
        # while it is still in progress.
        with platform_compat.file_lock(
            lock_fh.fileno(),
            exclusive=True,
            required=True,
            timeout=_STAGING_LOCK_TIMEOUT if timeout is None else timeout,
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
    on timeout. ``npm run build`` (website/package.json) runs several processes,
    so killing only npm would leave a survivor that renames a late build over
    ``website/dist`` after the lock releases, while a peer is staging it.

    An exit of 0 is not taken as proof that a bundle was published: the build
    must also have replaced ``website/dist/index.html``, which a publish by
    rename and an in-place rewrite both do. Both readings come from the one
    filesystem, so no clock is compared with another.

    A revision whose Vite build still empties ``website/dist`` in place (an
    older Dev Fleet target) must not do that under a gateway serving it through
    the dev link, so the served bundle is moved to a copy first.
    """
    built_dist = website_dir / "dist"
    static_dist = proj_path / "src" / "kiro_crew" / "static" / "dist"
    if not _publishes_atomically(built_dist) and _links_to(static_dist, built_dist):
        _stage_dist_locked(built_dist, static_dist, log)
    before = _index_identity(built_dist)
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
        log(
            f"  ⚠️  Frontend build timed out after {_BUILD_TIMEOUT}s"
            " — dashboard may be stale"
        )
        return False
    if proc.returncode != 0:
        log("  ⚠️  Frontend build failed — dashboard may be stale")
        return False
    after = _index_identity(built_dist)
    if after is None or after == before:
        log(f"  ⚠️  The build exited 0 but published no new {built_dist} — not staging")
        return False
    return _stage_dist_locked(built_dist, static_dist, log)


def _index_identity(dist: Path) -> Optional[tuple[int, int]]:
    """``(st_mtime_ns, st_ino)`` of ``dist/index.html``, or ``None`` if it is absent."""
    try:
        st = (dist / "index.html").stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_ino)


def build_and_stage(
    proj_path: "str | Path | None" = None,
    npm: str | None = None,
    log: Callable[[str], None] = _print_safe,
    git: str | None = None,
) -> bool:
    """Build this install's frontend and stage it, both under one lock.

    The entry point for callers that build an install they do NOT run
    in-process — notably Dev Fleet's Pull+Build. Holding the lock across the
    build is what makes the result safe to publish: ``npm run build`` swaps a
    new tree into ``website/dist``, so a peer flow staging concurrently would
    otherwise copy half of each.

    ``proj_path`` accepts a string because the callers that need it are
    out-of-process and pass it through ``argv``. ``npm`` names the executable to
    run, so a caller that resolved a trusted path passes it rather than having it
    re-resolved here. ``git`` likewise names the git executable for the read-only
    build-source fingerprint: the Dev Fleet sync passes its trusted-bin absolute
    path so the fingerprint's git calls never depend on a PATH search. Returns
    ``True`` when ``static/dist`` holds the newly built bundle.
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
    git_bin = git or shutil.which("git") or "git"
    try:
        with _staging_lock(root / "src" / "kiro_crew" / "static"):
            staged = _npm_build_and_stage_locked(website_dir, root, npm_bin, log)
            if staged:
                _write_build_source_fingerprint(root, git_bin, log)
            return staged
    except OSError as exc:
        log(f"  ⚠️  Could not acquire the static/dist staging lock: {exc}")
        return False


#: Records WHICH ``website/`` source the currently staged bundle was built from.
#: Written beside the staged dist on every successful build+stage, and read by
#: Dev Fleet's backend-only-sync skip: the skip is only safe when this equals the
#: source the sync will end up with. It is the fingerprint the skip needs
#: to distinguish "the build is already current" from a STALE tree left when a
#: prior frontend sync merged new source but its ``npm ci`` failed and the
#: transaction restored the old node_modules -- a case where the subtree stops
#: changing yet the staged bundle was never built from it.
_BUILD_SOURCE_FINGERPRINT = "kirocrew-build-source.txt"


def _write_build_source_fingerprint(root: Path, git_bin: str, log: Callable[[str], None]) -> None:
    """Stamp the git tree id of ``website/`` at HEAD beside the staged dist.

    The git tree object id of ``website/`` is the exact identity of the built
    source: it changes iff any tracked file under ``website/`` changes, and it
    costs one ``git rev-parse``. Written to ``static/dist`` so it travels with
    the bundle and is swept/replaced with it. Best-effort: a failure to stamp
    leaves no fingerprint, and a missing fingerprint makes the skip decision fall
    through to a rebuild (the safe direction), so this never blocks a build.

    ``git_bin`` is the git executable to run -- the Dev Fleet sync passes its
    trusted-bin absolute path, so these read-only calls do not depend on a PATH
    search. Both calls are fixed list-argv, shell-free, and carry no
    agent-supplied component; ``root`` is the operator's own registered checkout.

    STAMPED ONLY WHEN ``website/`` IS CLEAN. ``HEAD:website`` names the committed
    tree, but the build compiles the WORKING tree -- so if ``website/`` carried
    uncommitted edits, the bundle was built from content ``HEAD:website`` does
    not describe. Stamping anyway would let a later backend-only sync skip on a
    fingerprint that matches HEAD while the dirty edit that was actually built
    has since been reverted, serving a bundle built from content no longer on
    disk. So a dirty ``website/`` writes NO fingerprint, and the next sync
    rebuilds. The sync's own build runs after a ``merge --ff-only`` and is clean;
    this guard covers the other callers (pod provision, dashboard update) and any
    path that could build a dirty tree.
    """
    static_dist = root / "src" / "kiro_crew" / "static" / "dist"
    try:
        dirty = subprocess.run(  # nosec B603 - argv list, no shell
            [git_bin, "-C", str(root), "status", "--porcelain", "--", "website"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if dirty.returncode != 0 or (dirty.stdout or b"").strip():
            # Non-zero: cannot establish cleanliness. Non-empty: website/ has
            # uncommitted changes, so HEAD:website does not describe what was
            # built. Either way, leave no fingerprint -> the next sync rebuilds.
            log(
                "  ⚠️  website/ was not clean at build time; not fingerprinting "
                "the bundle, so the next backend-only sync will rebuild"
            )
            return
        proc = subprocess.run(  # nosec B603 - argv list, no shell
            [git_bin, "-C", str(root), "rev-parse", "HEAD:website"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            log(
                "  ⚠️  Could not fingerprint the built frontend source; the next "
                "backend-only sync will rebuild rather than skip"
            )
            return
        tree_id = proc.stdout.decode(errors="replace").strip()
        if not tree_id:
            # An empty rev-parse output proves nothing; leave no fingerprint so
            # the next sync rebuilds rather than trusting an empty tree id.
            return
        # Through the dev link this lands in website/dist, which the next build
        # (hand-run ones included) replaces: the fingerprint then goes with it,
        # and a missing fingerprint only ever means "rebuild".
        (static_dist / _BUILD_SOURCE_FINGERPRINT).write_text(tree_id, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(
            f"  ⚠️  Could not write the build source fingerprint ({exc}); the next "
            "backend-only sync will rebuild rather than skip"
        )


def _discard_path(path: Path) -> None:
    """Best-effort remove a file, symlink or directory.

    A staged-aside entry can be any of the three — ``static/dist`` is a link
    on a source install and a real tree once staged — and ``shutil.rmtree``
    refuses a link even though ``is_dir()`` follows it and returns True.

    The link half must be ``is_link_or_junction``, not ``is_symlink``: this
    module publishes ``static/dist`` itself via
    :func:`platform_compat.symlink_or_junction`, which falls back to a directory
    JUNCTION on Windows, and ``is_symlink`` reports False for one. A live
    junction would then reach the ``rmtree`` branch, whose refusal
    ``ignore_errors=True`` swallows — leaving the entry behind for good; a
    DANGLING junction answers False to all three and was never removed at all.
    ``unlink_link_or_junction`` detaches either shape without touching what it
    points at.
    """
    try:
        if platform_compat.is_link_or_junction(path):
            platform_compat.unlink_link_or_junction(path)
        elif path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _stage_dist(
    built_dist: Path,
    proj_path: Path,
    log: Callable[[str], None] = _print_safe,
    *,
    lock_timeout: Optional[float] = None,
) -> bool:
    """Publish a freshly built dist as the served ``static/dist``.

    ``static/dist`` is always switched by re-pointing a link, never by renaming
    or rewriting a served tree: to ``website/dist`` itself for a stock build
    that publishes atomically (the dev link), otherwise to an immutable copy in
    a fresh ``static/.dist.<id>`` (:func:`_stage_dist_locked`). The dashboard
    resolves ``static/dist`` on every request, so a running gateway follows
    either at once. A live dev link to ``built_dist`` needs no lock, so a stage
    with nothing to copy never waits on one or fails on one.

    Returns ``True`` when ``static/dist`` now serves the new bundle. Callers that
    treat staging as best-effort can keep ignoring the result — the failure is
    still logged — but a caller whose own success depends on staging (Dev Fleet's
    Pull+Build) must check it, because a preserved older bundle is no longer
    evidence that anything was staged.
    """
    static_dist = proj_path / "src" / "kiro_crew" / "static" / "dist"
    if _serves_through_dev_link(built_dist, static_dist):
        staged = _report_dev_link(built_dist, log)
        # Residue from an earlier copy is swept only by whoever holds the lock;
        # take it if it is free, and never wait for it here.
        with contextlib.suppress(OSError):
            with _staging_lock(static_dist.parent, 0):
                _sweep_staged_locked(static_dist)
        return staged
    # Staging alone takes the lock; callers that also BUILD must hold it across
    # both (see build_and_stage), since the build replaces the tree this copies.
    try:
        with _staging_lock(static_dist.parent, lock_timeout):
            return _stage_dist_locked(built_dist, static_dist, log)
    except OSError as exc:
        log(
            f"  ⚠️  Could not acquire the static/dist staging lock ({exc}) — a "
            "frontend build may hold it; static/dist was not changed"
        )
        return False


def _report_dev_link(built_dist: Path, log: Callable[[str], None]) -> bool:
    """The dev link already serves ``built_dist``: report it, nothing to copy."""
    reason = _incomplete_bundle_reason(built_dist)
    if reason:
        log(f"  ⚠️  {built_dist} is not a complete build ({reason}) — not staging")
        return False
    _log_nothing_to_copy(built_dist, log)
    return True


def _log_nothing_to_copy(built_dist: Path, log: Callable[[str], None]) -> None:
    log(f"  📦 static/dist links to the build at {built_dist} — nothing to copy")


def _is_staged_tree(path: Path, static_dist: Path) -> bool:
    """Whether ``path`` is one of the immutable copies staged beside ``static_dist``.

    Compared by identity, not by path string: a case-insensitive filesystem or a
    bind mount reaches one checkout through more than one spelling.
    """
    return path.name.startswith(_STAGED_PREFIX) and _same_dir(path.parent, static_dist.parent)


def _sweep_staged_locked(static_dist: Path) -> None:
    """Remove every staged copy, link and residue beside ``static_dist`` that it does not serve.

    Caller holds the staging lock, so nothing else is writing one. That covers
    a copy a killed stage left behind, a ``.dist.staging.*`` or
    ``.dist.previous.*`` tree (residue of a tree-rename stage), and a real
    ``static/dist`` retired when it was replaced by a link. Each is a whole
    bundle of untracked residue that makes the checkout read as dirty, which
    fail-closes Dev Fleet's prune.

    The served copy is recognised by identity (:func:`_same_dir`), so a second
    spelling of the checkout cannot make it look unserved. While ``static/dist``
    is a link that resolves nowhere, nothing is swept: which copy it meant
    cannot be told, and residue costs less than a deleted dashboard.
    """
    served = _live_link_target(static_dist)
    if served is None and platform_compat.is_link_or_junction(static_dist):
        return
    for entry in static_dist.parent.glob(_STAGED_PREFIX + "*"):
        if entry.name == _STAGING_LOCK_NAME:
            continue
        if (
            served is not None
            and not platform_compat.is_link_or_junction(entry)
            and _same_dir(entry, served)
        ):
            continue
        _discard_path(entry)


def _point_static_dist_at(static_dist: Path, target: Path) -> bool:
    """Make ``static_dist`` a link to ``target``. Caller holds the staging lock.

    POSIX swaps link for link in one ``rename``, so a reader sees the old target
    or the new one and never neither. Windows cannot rename one junction over
    another, so the old one is removed first: removing a junction does not touch
    the tree behind it, so no open handle refuses it and the gap is a single
    directory-entry operation. A real ``static/dist`` directory is renamed aside
    once, to a name the next sweep removes.

    The link always carries an absolute target: a relative one would be read
    against the link's own directory, not the caller's working directory.

    Returns whether a real directory was retired, which a gateway that resolved
    it once at start cannot follow. Raises ``OSError`` with ``static_dist`` as
    it was.
    """
    tmp = static_dist.with_name(f"{_STAGED_PREFIX}link-{os.getpid()}-{secrets.token_hex(4)}")
    platform_compat.symlink_or_junction(os.path.abspath(target), str(tmp))
    retired: Optional[Path] = None
    previous: Optional[Path] = None
    try:
        if platform_compat.is_link_or_junction(static_dist):
            if platform_compat.IS_WINDOWS:
                previous = Path(os.path.realpath(static_dist))
                platform_compat.unlink_link_or_junction(static_dist)
        elif os.path.lexists(static_dist):
            retired = static_dist.with_name(_staged_name())
            replace_with_retry(static_dist, retired)
        replace_with_retry(tmp, static_dist)
    except OSError:
        if not os.path.lexists(static_dist):
            # The rollback rides the same Windows rename-window retry as the
            # forward moves: a bare ``os.replace`` refused by a scanner's handle
            # leaves ``static/dist`` absent and the retired tree to the sweep.
            with contextlib.suppress(OSError):
                if retired is not None:
                    replace_with_retry(retired, static_dist)
                elif previous is not None:
                    platform_compat.symlink_or_junction(str(previous), str(static_dist))
        _discard_path(tmp)
        raise
    return retired is not None


def _links_to_a_staged_copy(static_dist: Path) -> bool:
    """Whether ``static_dist`` is a live link to one of the copies staged beside it."""
    target = _live_link_target(static_dist)
    return target is not None and _is_staged_tree(target, static_dist)


def _report_served_dir_gone(gone: bool, log: Callable[[str], None]) -> None:
    """Tell the operator when the directory ``static/dist`` served was just retired.

    A gateway that resolved that directory once at start (a revision whose build
    routes are static mounts) keeps answering 404 for every chunk until it
    restarts: a real ``static/dist`` renamed aside, or a copy it linked to that
    the sweep removed.
    """
    if gone:
        log(
            "  ⚠️  static/dist no longer serves the directory it did: restart a "
            "gateway already running from this checkout if its dashboard comes up blank"
        )


def _staged_name() -> str:
    """A fresh ``static/.dist.<id>`` name: unique per stage, never reused."""
    return f"{_STAGED_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"


def _stage_dist_locked(
    built_dist: Path,
    static_dist: Path,
    log: Callable[[str], None],
) -> bool:
    """Link or copy, re-point ``static/dist``, then sweep. Caller holds the staging lock.

    A stock in-tree build that publishes atomically is served through the dev
    link to ``built_dist``: kept if it is already there, made otherwise. Every
    other build -- an edition bundle, an older target revision whose Vite build
    still writes ``website/dist`` in place -- is copied into a fresh
    ``static/.dist.<id>`` and ``static/dist`` re-pointed at the copy, so no
    later build can rewrite what is served.

    Re-pointing away from a copy sweeps it. A gateway of the older target
    revision pinned that copy at start, so that re-stage says to restart it;
    an edition re-stage of a revision that publishes atomically does not, as
    its gateway resolves ``static/dist`` per request.
    """
    _sweep_staged_locked(static_dist)
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
    if not edition_configured() and _publishes_atomically(built_dist):
        if _links_to(static_dist, built_dist):
            _log_nothing_to_copy(built_dist, log)
            return True
        try:
            retired = _point_static_dist_at(static_dist, built_dist.resolve())
        except OSError as exc:
            # A gateway starting up links without the lock: if that is what
            # landed, it is the link wanted. Never copy over it.
            if _links_to(static_dist, built_dist):
                _log_nothing_to_copy(built_dist, log)
                return True
            log(f"  ⚠️  Could not link static/dist to {built_dist} ({exc}); copying instead")
        else:
            _sweep_staged_locked(static_dist)
            log(f"  📦 Linked static/dist → {built_dist}")
            _report_served_dir_gone(retired, log)
            return True
    staged = static_dist.parent / _staged_name()
    try:
        shutil.copytree(built_dist, staged)
        name, rule = _STAGED_COPY_IGNORE
        (staged / name).write_text(rule, encoding="utf-8")
    except OSError as exc:
        log(f"  ⚠️  Could not copy static/dist: {exc}")
        _discard_path(staged)
        return False
    reason = _incomplete_bundle_reason(staged)
    if reason:
        # The source passed its pre-copy check but changed while being read — a
        # peer flow's build replacing website/dist mid-copy. Serving this would
        # replace a valid bundle with a partial one.
        log(f"  ⚠️  Staged copy is incomplete ({reason}) — not publishing")
        _discard_path(staged)
        return False
    left_a_pinned_copy = not _publishes_atomically(built_dist) and _links_to_a_staged_copy(
        static_dist
    )
    try:
        retired = _point_static_dist_at(static_dist, staged)
    except OSError as exc:
        log(f"  ⚠️  Could not stage static/dist: {exc}")
        _discard_path(staged)
        return False
    _sweep_staged_locked(static_dist)
    log(f"  📦 Staged static/dist ← {built_dist}")
    _report_served_dir_gone(retired or left_a_pinned_copy, log)
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
    log: Callable[[str], None] = _print_safe,
    *,
    lock_timeout: Optional[float] = None,
) -> None:
    """Stage an ALREADY-built ``website/dist`` into the served ``static/dist``.

    The public seam for callers that run the npm build themselves and only need
    the staging half — Dev Fleet's Pull+Build, which drives each build step as
    its own audited subprocess and so cannot call
    :func:`build_frontend_sync`'s all-in-one path.

    Without this step a Pull+Build leaves the new bundle in ``website/dist``
    while a gateway serving a staged copy keeps serving the old one. Through the
    dev link (:func:`ensure_dev_dist_symlink`) there is nothing to copy, and the
    step reports that.

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
    if not _stage_dist(built, proj, log, lock_timeout=lock_timeout):
        raise RuntimeError(
            "dist staging failed; static/dist serves what it held before the "
            f"attempt (built dist: {built})"
        )


def build_frontend_sync(
    proj_path: Path,
    log: Callable[[str], None] = _print_safe,
) -> None:
    """Build the in-tree ``website/`` frontend and stage it (synchronous).

    Runs ``npm ci`` (falling back to ``npm install`` when there is no
    lockfile) then ``npm run build`` in ``<proj>/website``, then stages
    ``website/dist`` as ``src/kiro_crew/static/dist`` (:func:`_stage_dist_locked`:
    the dev link, or a link to a fresh ``static/.dist.<id>`` copy). Graceful no-op when
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
    # The cost is real and deliberate: a peer waits for an install (up to
    # _INSTALL_TIMEOUT) rather than only for a build. Two frontend builds on one
    # checkout are mutually destructive, so waiting is the correct outcome.
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
                    log(
                        f"  ⚠️  Frontend npm install timed out after {_INSTALL_TIMEOUT}s"
                        " — the dependency tree was left as it was"
                    )
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
                # SIGINT goes to the foreground process group, and npm is not
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
    timeouts + kill-on-timeout, then stages ``website/dist`` as
    ``src/kiro_crew/static/dist`` (:func:`_stage_dist_locked`: the dev link, or
    a link to a fresh ``static/.dist.<id>`` copy). Graceful no-op when there is no
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
            _warn(
                f"Frontend npm install timed out after {_INSTALL_TIMEOUT}s"
                " -- the dependency tree was left as it was"
            )
            return
        if npm_i.returncode != 0:
            await _offload(backup.rollback)
            _warn("Frontend npm install failed -- the dependency tree was left as it was")
            return
        await _offload(backup.commit)

        # Still inside the SAME holder, so this calls the _locked variant --
        # re-entering _staging_lock here would deadlock against ourselves. The
        # build swaps a new tree into website/dist, so a peer staging
        # concurrently would copy half of each.
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
        # still running `npm run build` (which swaps website/dist) and staging
        # it, and a peer would publish a bundle vite is mid-rewrite -- the mixed
        # bundle the lock exists to prevent. The lock is held OUTSIDE the worker, so
        # a cancelled await can release it early unless the future is tracked.
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


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m kiro_crew.frontend stage [REPO]``: stage a built ``website/dist``.

    The one entry point the build drivers (Makefile, make.ps1) and the docs use.
    Waits at most :data:`_CLI_STAGE_LOCK_TIMEOUT` for the staging lock, and
    refuses a directory that is not a Kiro Crew checkout before creating anything
    in it.
    """
    platform_compat.ensure_utf8_console()
    parser = argparse.ArgumentParser(prog="python -m kiro_crew.frontend")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="stage website/dist into src/kiro_crew/static/dist")
    stage.add_argument("repo", nargs="?", default=".", help="repository root (default: .)")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if not (repo / _DIR_NAME).is_dir() or not (repo / "src" / "kiro_crew").is_dir():
        _print_safe(f"error: {repo} is not a Kiro Crew checkout (no website/ and src/kiro_crew/)")
        return 1
    try:
        stage_built_dist(repo, lock_timeout=_CLI_STAGE_LOCK_TIMEOUT)
    except (RuntimeError, OSError) as exc:
        _print_safe(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
