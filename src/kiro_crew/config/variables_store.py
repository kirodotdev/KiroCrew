"""Storage for user-defined ``{{name}}`` variables, in their own file.

WHY THIS IS NOT IN ``config.json``
==================================

It was, and that placement was the root cause of most of this feature's review
history. ``KiroCrewConfig.save()`` serializes the MERGED config and replaces the
whole file, and ``to_dict()`` builds an explicit dict, so the file is a lossy
whole-document rewrite of exactly the keys the dataclass models. For a map whose
only legitimate writer is a dedicated endpoint, that produced a genuine trilemma —
every possible behaviour for the variables slot during an unrelated ``save()`` is
wrong in a different way:

* serialize the merged value  -> overwrites a base value the overlay shadowed, and
  the shadowed value is not in the merged view at all, so it is unrecoverable;
* preserve it while holding the config lock -> ``save()`` is a sync method called
  from 13 async call sites, so a contended POSIX flock stalls the event loop;
* preserve it with an unlocked read -> the read-then-write window silently drops a
  variables write that already returned 200 to its caller.

Moving the data out deletes the trilemma rather than choosing among its three
positions. ``save()`` no longer serializes variables at all, so there is nothing to
preserve, no lock to interact with, and no window. It also removes the overlay
subtraction problem, the overlay-owned-key refusal, and the deleted-workspace
resurrection window — all of which existed only because this map lived inside a
document with a second overlay layer and a whole-file writer.

The cost, stated plainly: variables are no longer part of ``config.json``, so they
are not covered by whatever backs that file up, and a hand-edit goes here instead.
There is no migration path because no released version stored them anywhere.

SHAPE
=====

One flat document, one writer, three scopes::

    {
      "global":     {"NAME": "value"},
      "workspaces": {"ops": {"NAME": "value"}},
      "crews":      {"reviewer": {"NAME": "value"}}
    }

Session scope is deliberately absent: it is per-turn state, never persisted.

READ is tolerant, WRITE is strict. An unreadable or malformed store resolves to no
variables rather than raising, because a broken store must not take the gateway down
over an optional feature. A WRITE refuses a malformed container instead of replacing
it, because the operator's hand-written value is the only copy there is.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCOPE_GLOBAL = "global"
SCOPE_WORKSPACE = "workspace"
SCOPE_CREW = "crew"

# The scope's key in the stored document. Global is a flat map; the other two are
# maps of name -> map, so they need a container key.
_CONTAINER = {SCOPE_WORKSPACE: "workspaces", SCOPE_CREW: "crews"}

_STORE_DIR = "variables"
_STORE_NAME = "variables.json"

# mtime-keyed read cache. KiroCrewConfig.load() applies the store on every call and
# load() runs on the event loop, so an uncached read meant a file read plus a JSON
# parse per load -- for a large store, a measurable stall. Keyed on the same signature
# shape config.json's own cache uses (mtime_ns + size + mode), so any edit,
# truncation or replacement busts it, and a missing file has a distinct sentinel so
# create and delete bust it too.
#
# The residual is one stat() per config load on the loop. That is the same class of
# cost load() already pays to read config.json itself, so this adds no new kind of
# blocking work -- but it is not zero, and a caller that needs a guaranteed-fresh read
# without a stat does not exist today.
_cache: tuple[tuple, dict] | None = None

# Bumped by every invalidation. A reader captures this BEFORE it reads and refuses to
# publish if it changed, which closes a race the fingerprint alone cannot: a reader
# that started before a write can finish after it, and would otherwise install its
# pre-write document under a signature that now matches the post-write file — so every
# later reader would be served stale values indefinitely, not just once.
_generation = 0


def _fingerprint(path: Path) -> tuple:
    """Cheap signature of the store file; changes whenever it is edited."""
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size, st.st_mode)
    except OSError:
        return (str(path), None)


def invalidate_cache() -> None:
    """Drop the read cache and advance the generation.

    Called by ``patch_store`` after a write rather than relying on the fingerprint
    alone: an atomic replace can land inside the same mtime granularity as the read
    that preceded it, and a stale hit would then serve the pre-write document.

    The generation bump is the second half of that, and it covers the harder case —
    a reader already IN FLIGHT when the write lands. Clearing ``_cache`` does nothing
    about a reader that is about to assign to it.
    """
    global _cache, _generation
    _cache = None
    _generation += 1


class MalformedStore(Exception):
    """A container the write would have to replace holds a non-mapping.

    Refused rather than coerced: replacing it would discard whatever the operator
    hand-wrote, and there is no second copy to restore from. Carries the dotted path
    so the caller can tell the operator what to repair.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def store_path() -> Path:
    """Location of the store, in its OWN directory under the config root.

    A directory rather than a bare file beside ``config.json``, because the fence in
    ``security.py`` protects a path by name and this file is not written alone:
    ``update_config_locked`` creates a predictable ``<path>.lock`` sidecar, and
    ``write_config_atomically`` stages a temp inode in the same directory before
    renaming. A leaf entry covers the target and leaves both of those unfenced, so an
    agent watching the directory could write the staging inode or the lock. Fencing
    the whole directory covers the target, the lock and the temp files together —
    the same reason the ``.vault`` entry is a directory entry.

    Derived from ``config_path()`` rather than hardcoded so a relocated or
    test-redirected config root carries the store with it. Imported lazily because
    this module is a leaf and ``loader`` imports it.
    """
    from kiro_crew.config.loader import config_path

    return config_path().parent / _STORE_DIR / _STORE_NAME


def read_store() -> dict[str, Any]:
    """Read the raw store document. Never raises.

    Every failure resolves to an empty document, which resolves to no variables. A
    malformed store must not break a gateway boot over an optional feature; the
    write path is where a malformed value is reported, because that is where it can
    be acted on and where silence would destroy data.
    Cached on the file's signature (see ``_fingerprint``): this runs on every config
    load, and those run on the event loop, so an uncached read meant a file read plus
    a JSON parse per load. Returns a deep copy so a caller cannot mutate the cached
    document — ``_apply_variables_store`` hands these maps to config objects, and an
    alias would let one session's edit leak into every later reader.
    """
    global _cache
    path = store_path()
    fingerprint = _fingerprint(path)
    if _cache is not None and _cache[0] == fingerprint:
        return copy.deepcopy(_cache[1])
    # Captured BEFORE the read. If a write invalidates while this read is in flight,
    # the document in hand is pre-write and must not be published — otherwise it lands
    # under a signature matching the post-write file and every later reader is served
    # stale values. Dropping the publish costs one re-read; publishing costs
    # correctness until the next write.
    generation = _generation
    doc = _read_uncached(path)
    if generation == _generation:
        _cache = (fingerprint, doc)
    return copy.deepcopy(doc)


def _read_uncached(path: Path) -> dict[str, Any]:
    """The read itself, split out so the cache wrapper stays legible."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "variables store at %s is unreadable (%s); resolving no variables. "
            "Repair or remove that file to restore them.",
            path.name,
            exc.__class__.__name__,
        )
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "variables store at %s is not a JSON object; resolving no variables.",
            path.name,
        )
        return {}
    return raw


def _clean_pairs(raw: object, where: str) -> dict[str, str]:
    """Coerce one scope's map to validated str->str pairs.

    Delegates to the loader's ``coerce_variables`` so validation lives in exactly one
    place — the same name grammar and value-length cap the write path enforces, and
    the same drop-one-pair-not-the-scope tolerance. Imported lazily: the loader
    imports this module, so a module-level import here would close a cycle.
    """
    from kiro_crew.config.loader import coerce_variables

    return coerce_variables(raw, where)


def global_values(doc: dict[str, Any] | None = None) -> dict[str, str]:
    """Global-scope pairs."""
    doc = read_store() if doc is None else doc
    return _clean_pairs(doc.get(SCOPE_GLOBAL), SCOPE_GLOBAL)


def scoped_values(scope: str, doc: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    """All named maps for ``workspace`` or ``crew`` scope."""
    container = _CONTAINER[scope]
    doc = read_store() if doc is None else doc
    raw = doc.get(container)
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("variables store: %s is not an object; ignoring it", container)
        return {}
    return {
        name: _clean_pairs(pairs, f"{container}.{name}")
        for name, pairs in raw.items()
        if isinstance(name, str)
    }


def _mutate(
    doc: dict[str, Any],
    *,
    scope: str,
    name: str,
    values: dict[str, str],
    removals: list[str],
) -> dict[str, Any]:
    """Apply a per-KEY patch to the document read under the lock.

    Named keys only: a key nobody mentioned is never read and never rewritten, so
    two concurrent writers touching different keys cannot lose each other's edits,
    and there is no whole-scope echo to go stale.

    A container that is ABSENT is created — that is the legitimate first write. A
    container that is PRESENT but not a mapping is refused, because replacing it
    would destroy the only copy of what the operator wrote.

    "Absent" means the key is MISSING, tested by membership. ``.get()`` returning None
    conflates a missing key with an explicit ``null``, and a hand-edited
    ``{"global": null}`` is present operator data — treating it as absent overwrote
    it, which is exactly what the malformed refusal exists to prevent. All three
    container levels use membership, for the same reason.
    """
    if scope == SCOPE_GLOBAL:
        if SCOPE_GLOBAL not in doc:
            target: dict = {}
            doc[SCOPE_GLOBAL] = target
        elif isinstance(doc[SCOPE_GLOBAL], dict):
            target = doc[SCOPE_GLOBAL]
        else:
            raise MalformedStore(SCOPE_GLOBAL)
    else:
        container = _CONTAINER[scope]
        if container not in doc:
            holder: dict = {}
            doc[container] = holder
        elif isinstance(doc[container], dict):
            holder = doc[container]
        else:
            raise MalformedStore(container)
        if name not in holder:
            target = {}
            holder[name] = target
        elif isinstance(holder[name], dict):
            target = holder[name]
        else:
            raise MalformedStore(f"{container}.{name}")

    for key, value in values.items():
        target[key] = value
    for key in removals:
        target.pop(key, None)
    return doc


def patch_store(
    *,
    scope: str,
    name: str = "",
    values: dict[str, str] | None = None,
    removals: list[str] | None = None,
) -> dict[str, Any]:
    """Apply a per-key patch under the store's own lock. Blocking; call off-loop.

    Routed through ``update_config_locked`` so the read and the write are one
    transaction against the store's advisory lock. That helper is reused rather than
    re-implemented so this file inherits its atomic replace, its mode preservation,
    and its symlink handling.

    This is the ONLY writer. ``KiroCrewConfig.save()`` does not touch this file,
    which is the entire point of the file existing.
    """
    if scope not in (SCOPE_GLOBAL, SCOPE_WORKSPACE, SCOPE_CREW):
        raise ValueError(f"unknown variables scope: {scope!r}")
    if scope != SCOPE_GLOBAL and not name:
        raise ValueError(f"{scope} scope requires a name")

    from kiro_crew.config.loader import update_config_locked

    vals = dict(values or {})
    dels = list(removals or [])

    def _apply(current: dict) -> dict:
        return _mutate(current, scope=scope, name=name, values=vals, removals=dels)

    # The directory is created here, not at import: it must exist before
    # update_config_locked places its lock sidecar, and creating it on a read path
    # would make a plain resolution write to disk. 0o700 so the fenced directory is
    # not world-listable either.
    path = store_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        logger.debug("could not create the variables store directory", exc_info=True)

    # on_corrupt="fail": a corrupt store must NOT be reset to {} by a write, which
    # would delete every variable at every scope to service one patch. read_store()
    # is the tolerant path; this one refuses and the caller reports it.
    #
    # stamp_meta=False: this is not a config document and must not grow config's
    # bookkeeping keys — the shape here is exactly the three scope containers.
    result = update_config_locked(store_path(), mutate=_apply, stamp_meta=False, on_corrupt="fail")
    # Before anything else: a reader arriving after this write must not take a cache
    # hit on the pre-write document.
    invalidate_cache()
    try:
        os.chmod(store_path(), 0o600)
    except OSError:
        # Mode is defence in depth, not the security boundary — values are declared
        # non-secret. A filesystem that refuses chmod must not fail the write.
        logger.debug("could not tighten mode on the variables store", exc_info=True)
    return result if isinstance(result, dict) else {}
