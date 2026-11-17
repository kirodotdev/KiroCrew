"""Node's on-disk compile cache for the agent engine.

The agent engine is a Node.js process running one ~15 MB JavaScript bundle.
Without a cache, every engine start parses and compiles that bundle. Node 22
keeps a compile cache when ``NODE_COMPILE_CACHE`` names a directory: a process
that exits writes it, and a later start reads it instead of compiling.

A cache entry is code the engine runs, and Node checks an entry only against
the source's length and CRC values anyone can compute. So the cache must not
live where a sandboxed agent can write, or an agent could plant code that every
later engine runs. It lives under the ``engines`` leaf, which the OS sandbox
mounts READ-ONLY (``sandbox_leaves._CREW_READONLY_LEAVES``), one directory per
route so a writer on one route cannot reach the other:

``<data home>/engines/node-compile/kas-bundle``
    Crew's own engine bundle (``agent.kas_bundle``). Its engine is sealed too,
    so the gateway fills this cache itself (:func:`ensure_engine_bundle_cache`),
    by running that sealed bundle once INSIDE the OS sandbox (strict mode, one
    writable scratch under the runtime parent, everything else read-only or
    hidden) and then relocating the entries the engine wrote in its scratch into
    this read-only cache. The engine never runs outside the sandbox; with no
    sandbox backend the prewarm is skipped (fail closed). That run loads modules
    only from inside the engine bundle directory (:data:`_CONFINE_BOOTSTRAP`) as
    a second layer, so a module an agent planted in a parent ``node_modules``
    cannot run in it. Sandboxed engines only read the cache; Node ignores a write
    that fails.

``<data home>/engines/node-compile/kiro-cli``
    kiro-cli's own engine. Its binary and bundle sit in the user's home, which
    a sandboxed agent can write, so the gateway never runs them outside the
    sandbox to fill a cache. The variable is set only when the effective
    sandbox tier is ``off``: then nothing is sealed, the engine fills the cache
    itself, and a planted entry gives an agent nothing it did not already have.

Fill-only: a value the environment already carries (an empty one included) is
the operator's, and ``NODE_DISABLE_COMPILE_CACHE`` turns it off. Nothing here
may fail a spawn: any error leaves the variable unset.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import MutableMapping
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["apply_node_compile_cache", "ensure_engine_bundle_cache"]

NODE_COMPILE_CACHE_ENV = "NODE_COMPILE_CACHE"
#: Node's own switch that disables the cache even when a directory is named.
NODE_DISABLE_COMPILE_CACHE_ENV = "NODE_DISABLE_COMPILE_CACHE"
_CACHE_LEAF = "node-compile"
ROUTE_ENGINE_BUNDLE = "kas-bundle"
ROUTE_KIRO_CLI = "kiro-cli"
#: The engine exits on stdin EOF after loading; a slow host gets this long.
_PREWARM_TIMEOUT_SECS = 120
_STAMP_PREFIX = ".prewarmed-"

#: Relocation bounds. The gateway moves the compile cache the sandboxed engine
#: wrote in its scratch into the read-only ``engines`` leaf. V8 does not integrity-
#: check a cache entry (only the source's length and computable CRCs), so the
#: gateway must refuse to relocate anything that is not a plain, bounded,
#: well-named cache file -- a symlink, a hardlink, an over-size or over-count tree,
#: or a stray name -- and discard the whole batch rather than relocate a doubtful
#: one. This is defense-in-depth: the scratch is gateway-owned under the sandbox's
#: read-only ``run`` parent, so a sandboxed agent cannot write it in the first place.
_CACHE_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._+-]*$")
_CACHE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_CACHE_FILE_BYTES = 16 * 1024 * 1024
_MAX_CACHE_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_CACHE_FILES = 8192
_MAX_CACHE_DEPTH = 8

#: A CommonJS module Node loads (``--require``) before the engine during the
#: prewarm ONLY, as a SECOND layer behind the OS sandbox: the prewarm runs the
#: engine inside a strict sandbox, and this bootstrap defends even if that
#: boundary were weakened. Node's default resolution walks up from the bundle's
#: real path into every parent ``node_modules`` directory, and also consults
#: ``$HOME/.node_modules`` and ``$HOME/.node_libraries``. This filters the final
#: lookup list of both ``_nodeModulePaths`` (the parent walk) and
#: ``_resolveLookupPaths`` (which adds the home paths) down to the engine bundle
#: root, so a bare name the bundle does not vendor -- ``long`` and ``bufferutil``
#: are optional and absent -- fails to resolve instead of reaching any directory
#: outside the bundle. The engine already tolerates that MODULE_NOT_FOUND, so it
#: still starts. Real engine spawns run inside the sandbox and are not given this.
_CONFINE_BOOTSTRAP = """'use strict';
const Module = require('module');
const path = require('path');
const fs = require('fs');
let main = process.argv[1] || '';
try { main = fs.realpathSync(main); } catch (e) {}
const sep = path.sep;
const at = main.indexOf(sep + 'node_modules' + sep);
const root = at >= 0 ? main.slice(0, at) : path.dirname(main);
const prefix = root + sep;
function inside(dir) { return dir === root || dir.indexOf(prefix) === 0; }
const nodeModulePaths = Module._nodeModulePaths;
Module._nodeModulePaths = function (from) {
  return nodeModulePaths.call(this, from).filter(inside);
};
const resolveLookupPaths = Module._resolveLookupPaths;
Module._resolveLookupPaths = function (request, parent) {
  const paths = resolveLookupPaths.call(this, request, parent);
  return Array.isArray(paths) ? paths.filter(inside) : paths;
};
"""
_CONFINE_BOOTSTRAP_NAME = "confine.cjs"

#: The name of the background prewarm thread; the test floor joins it by this name.
PREWARM_THREAD_NAME = "node-compile-prewarm"
_prewarm_lock = threading.Lock()
_prewarm_started: set[str] = set()


def node_compile_cache_dir(route: str) -> str:
    """``<data home>/engines/node-compile/<route>``."""
    from kiro_crew.acp.kas_bundle import KAS_BUNDLE_ENGINES_LEAF
    from kiro_crew.config.paths import data_home

    return str(data_home() / KAS_BUNDLE_ENGINES_LEAF / _CACHE_LEAF / route)


def _writable_dir(path: str) -> bool:
    """Create *path* if missing; True only when it is a directory we can write."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return False
    return os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK)


def _sandbox_is_off() -> bool:
    """True only when the tier a spawn will actually get is ``off``."""
    from kiro_crew.sandbox_tiers import configured_sandbox_mode, effective_sandbox_mode

    return effective_sandbox_mode(configured_sandbox_mode()) == "off"


def apply_node_compile_cache(env: MutableMapping[str, str], *, engine_bundle: bool = False) -> None:
    """Set ``NODE_COMPILE_CACHE`` in *env* when it is unset and the route allows it.

    *engine_bundle* is True for a spawn of Crew's own engine bundle. Mutates
    *env* in place. Does file IO, so callers run it off the event loop, as they
    already do for the other kiro-cli env defaults.
    """
    if NODE_COMPILE_CACHE_ENV in env or NODE_DISABLE_COMPILE_CACHE_ENV in env:
        return
    try:
        if engine_bundle:
            path = node_compile_cache_dir(ROUTE_ENGINE_BUNDLE)
            # Only the gateway's prewarm creates it; a sandboxed engine reads it.
            if os.path.isdir(path):
                env[NODE_COMPILE_CACHE_ENV] = path
            return
        if not _sandbox_is_off():
            return
        path = node_compile_cache_dir(ROUTE_KIRO_CLI)
    except Exception:  # a data home or tier that cannot be resolved must not break a spawn
        logger.debug("node compile cache: route unavailable", exc_info=True)
        return
    if not _writable_dir(path):
        logger.debug("node compile cache: %s is not writable, leaving it unset", path)
        return
    env[NODE_COMPILE_CACHE_ENV] = path


def _stamp_name(argv: list[str]) -> str:
    """A name that changes whenever the engine's node or bundle file changes."""
    parts = []
    for item in argv:
        try:
            st = os.stat(item)
        except OSError:
            parts.append(item)
            continue
        parts.append(f"{item}\0{st.st_size}\0{st.st_mtime_ns}")
    return _STAMP_PREFIX + hashlib.sha256("\0\0".join(parts).encode("utf-8")).hexdigest()[:16]


def _confined_prewarm_argv(argv: list[str], bootstrap: str) -> list[str]:
    """``argv`` with a ``--require`` of the confinement bootstrap among the node flags.

    The flag goes right after the node binary so it precedes the engine script,
    which is where Node reads ``--require``. The engine's own flags and arguments
    keep their order.
    """
    return [argv[0], "--require", bootstrap, *argv[1:]]


def _validated_cache_entry(cache_build: str) -> list[str]:
    """The tag directories under *cache_build* safe to relocate, or [] on any doubt.

    Checks every entry with ``lstat`` (never following a link): each top-level entry
    is a real directory with a Node compile-cache version-tag name, and everything
    under it is a real subdirectory or a plain regular file (one link, bounded size,
    plain name), within a depth, count and total-size budget. Any violation returns
    ``[]`` so the caller relocates nothing -- a tampered or surprising scratch is
    discarded whole, never half-copied into the read-only cache.
    """
    try:
        names = sorted(os.listdir(cache_build))
    except OSError:
        return []
    total = 0
    count = 0
    for name in names:
        if not _CACHE_TAG_RE.match(name):
            return []
        tag = os.path.join(cache_build, name)
        try:
            if not stat.S_ISDIR(os.lstat(tag).st_mode):
                return []
        except OSError:
            return []
        base_depth = tag.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(tag):
            if root.rstrip(os.sep).count(os.sep) - base_depth > _MAX_CACHE_DEPTH:
                return []
            for entry in (*dirs, *files):
                path = os.path.join(root, entry)
                try:
                    st = os.lstat(path)
                except OSError:
                    return []
                if stat.S_ISDIR(st.st_mode):
                    continue  # a real subdirectory; os.walk (followlinks=False) descends it
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    return []  # symlink, hardlink, fifo, device, socket
                if not _CACHE_NAME_RE.match(entry):
                    return []
                if st.st_size > _MAX_CACHE_FILE_BYTES:
                    return []
                total += st.st_size
                count += 1
                if count > _MAX_CACHE_FILES or total > _MAX_CACHE_TOTAL_BYTES:
                    return []
    return names


def _relocate_prewarm_cache(cache_build: str, cache: str) -> bool:
    """Merge validated compile-cache files from the scratch into the engines cache.

    Returns True when at least one cache file is in *cache* afterward. Validation
    refuses the whole batch on any doubt. Files are merged at the FILE level, not
    by moving the tag directory whole: a bundle update keeps the same Node runtime
    tag directory but produces new content-hashed entry files, so moving only when
    the tag dir is absent would discard the updated build's cache. An entry whose
    name is already present is identical (Node names each by content hash), so it
    is left as is. ``os.rename`` is atomic and same-filesystem (scratch and cache
    both sit under the data home).
    """
    names = _validated_cache_entry(cache_build)
    if not names:
        return False
    present = False
    for name in names:
        src_tag = os.path.join(cache_build, name)
        dest_tag = os.path.join(cache, name)
        try:
            for root, _dirs, files in os.walk(src_tag):
                rel = os.path.relpath(root, src_tag)
                dest_root = dest_tag if rel == "." else os.path.join(dest_tag, rel)
                for fname in files:
                    dest_file = os.path.join(dest_root, fname)
                    if os.path.exists(dest_file):
                        present = True
                        continue  # identical: Node names each entry by content hash
                    os.makedirs(dest_root, mode=0o700, exist_ok=True)
                    os.rename(os.path.join(root, fname), dest_file)
                    present = True
        except OSError:
            logger.info("node compile cache: could not relocate %s", name, exc_info=True)
    return present


def prewarm_engine_bundle_cache(argv: list[str], cache: str | None = None) -> bool:
    """Run the sealed engine once, inside the OS sandbox, so Node writes its compile cache.

    The engine NEVER runs outside the OS sandbox. The gateway wraps the spawn
    with :func:`kiro_crew.sandbox_spawn.sandboxed_spawn_argv` in ``strict`` mode:
    the engine's own sealed bundle stays readable (the ``engines`` leaf is mounted
    read-only), one scratch dir under ``<data home>/run`` is the only writable
    path, and the home, credential and workspace trees are hidden. With no sandbox
    backend the wrap raises and this returns False (fail closed), never a spawn
    outside the sandbox. The engine writes its compile cache into that scratch; the
    gateway -- not the engine -- then relocates the entries into the read-only
    ``engines`` cache *cache*, which the sandbox never let the engine write. stdin
    is at EOF so the engine loads and exits. True when the cache was written and
    stamped.

    The ``--require`` bootstrap, the empty home and the unique scratch stay as a
    second layer, but the sandbox is the primary boundary.

    *cache* is the directory to fill. The background prewarm passes the one its
    caller resolved, so the worker thread never reads the data home itself.
    """
    from kiro_crew.config.paths import data_home
    from kiro_crew.sandbox import run_limited
    from kiro_crew.sandbox_spawn import sandboxed_spawn_argv

    if cache is None:
        cache = node_compile_cache_dir(ROUTE_ENGINE_BUNDLE)
    stamp = _stamp_name(argv)
    if not _writable_dir(cache):
        return False
    if os.path.exists(os.path.join(cache, stamp)):
        return True
    # The prewarm's one writable path. It sits under <data home>/run, the runtime
    # parent the sandbox lets a child's own scratch be carved writable; the engines
    # leaf cannot be carved, so the engine writes its cache here and the gateway
    # relocates it afterward. Unique per process, so two concurrent prewarms never
    # share a path.
    run_parent = os.path.join(str(data_home()), "run", "node-prewarm")
    try:
        os.makedirs(run_parent, mode=0o700, exist_ok=True)
        scratch = tempfile.mkdtemp(prefix="kirocrew-node-prewarm-", dir=run_parent)
    except OSError:
        logger.info("node compile cache: prewarm scratch unavailable", exc_info=True)
        return False
    cleanup: str | None = None
    try:
        bootstrap = os.path.join(scratch, _CONFINE_BOOTSTRAP_NAME)
        with open(bootstrap, "w", encoding="utf-8") as handle:
            handle.write(_CONFINE_BOOTSTRAP)
        cache_build = os.path.join(scratch, "cache")
        os.makedirs(cache_build, mode=0o700, exist_ok=True)
        env = {"PATH": os.defpath, "HOME": scratch, NODE_COMPILE_CACHE_ENV: cache_build}
        try:
            wrapped, spawn_env, cleanup = sandboxed_spawn_argv(
                _confined_prewarm_argv(argv, bootstrap),
                mode="strict",
                env=env,
                extra_writable_dirs=(scratch,),
            )
        except RuntimeError:
            # No OS sandbox backend. Fail closed: never run the engine unsandboxed.
            # The strict wrap raises SandboxUnavailableError (a RuntimeError) on a
            # host with no backend. first_party_fixed_argv is NOT passed on
            # purpose: that flag would let the wrap hand back an unconfined argv
            # on a backend-less host, which is the one thing this prewarm must
            # never do -- the cache it would write then runs in every later
            # sandboxed engine.
            logger.info("node compile cache: no sandbox backend, skipping prewarm", exc_info=True)
            return False
        # run_limited applies the kernel RLIMIT ceiling AFTER exec (via the spawn
        # shim), so this synchronous spawn from a gateway worker thread does not
        # fork() the multi-threaded gateway through a preexec_fn. Limits are the
        # same the shared preexec would set (the default "tool" profile).
        run_limited(
            wrapped,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=spawn_env,
            cwd=scratch,
            timeout=_PREWARM_TIMEOUT_SECS,
            check=True,
        )
        # The engine ran sandboxed and wrote its cache into the scratch. The
        # gateway (trusted, not the engine) relocates the entries into the
        # read-only engines cache, validating each one and discarding the whole
        # batch on any doubt (V8 does not integrity-check a cache entry).
        if not _relocate_prewarm_cache(cache_build, cache):
            return False
    except (OSError, subprocess.SubprocessError):
        logger.info("node compile cache: engine prewarm failed", exc_info=True)
        return False
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
        shutil.rmtree(scratch, ignore_errors=True)
    if not any(entry.is_dir() for entry in Path(cache).iterdir()):
        return False
    try:
        Path(cache, stamp).touch(mode=0o600)
    except OSError:
        return False
    return True


def ensure_engine_bundle_cache(argv: list[str]) -> None:
    """Start one background prewarm per engine build per gateway process. Never raises.

    The session that triggers it starts without a cache; later ones read it.
    """
    try:
        stamp = _stamp_name(argv)
        cache = node_compile_cache_dir(ROUTE_ENGINE_BUNDLE)
        if os.path.exists(os.path.join(cache, stamp)):
            return
    except Exception:
        logger.debug("node compile cache: prewarm check failed", exc_info=True)
        return
    with _prewarm_lock:
        if stamp in _prewarm_started:
            return
        _prewarm_started.add(stamp)
    try:
        threading.Thread(
            target=prewarm_engine_bundle_cache,
            args=(list(argv), cache),
            name=PREWARM_THREAD_NAME,
            daemon=True,
        ).start()
    except Exception:  # a thread that cannot start must not fail the spawn
        logger.debug("node compile cache: prewarm thread did not start", exc_info=True)
        with _prewarm_lock:
            _prewarm_started.discard(stamp)
