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
    so the gateway fills this cache itself, outside the sandbox
    (:func:`ensure_engine_bundle_cache`), by running that sealed bundle once with
    no input. Sandboxed engines only read it; Node ignores a write that fails.

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
import shutil
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


def prewarm_engine_bundle_cache(argv: list[str]) -> bool:
    """Run the sealed engine once with no input so Node writes its compile cache.

    Runs in the gateway, outside the sandbox: the cache directory is read-only
    inside it. The engine gets an empty home and no inherited environment, so
    it can reach no credential by name, and stdin at EOF, so it loads and exits.
    True when the cache was written and stamped.
    """
    cache = node_compile_cache_dir(ROUTE_ENGINE_BUNDLE)
    stamp = _stamp_name(argv)
    if not _writable_dir(cache):
        return False
    if os.path.exists(os.path.join(cache, stamp)):
        return True
    scratch = tempfile.mkdtemp(prefix="kirocrew-node-prewarm-")
    try:
        env = {"PATH": os.defpath, "HOME": scratch, NODE_COMPILE_CACHE_ENV: cache}
        subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=scratch,
            timeout=_PREWARM_TIMEOUT_SECS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.info("node compile cache: engine prewarm failed", exc_info=True)
        return False
    finally:
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
    threading.Thread(
        target=prewarm_engine_bundle_cache,
        args=(list(argv),),
        name="node-compile-prewarm",
        daemon=True,
    ).start()
