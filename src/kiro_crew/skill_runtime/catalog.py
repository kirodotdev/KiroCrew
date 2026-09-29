"""Which ``SKILL.md`` files exist for a root set, served without a walk on the caller's thread.

Three tiers answer ``SkillsLoader._iter``: the in-memory list, the same list
past its TTL with a re-walk queued, and the stored snapshot in the search index.
One daemon ``skill-catalog-refresh`` worker walks scopes serially; a generation
counter and the index epoch stop a walk that started before an invalidation from
publishing over it. Rows read back from the agent-writable index are admitted
here before they are served. The module also enumerates the configured roots in
precedence order, filters disabled apps' skills by owning app, expands an agent's
``skill://`` mapping into its available entries through the same provider-root
fence, and looks an enumerated name up.

The tree walk itself (``skills._iter_skill_files``) and the trust verdict that
selects a scope stay in the facade. All state is the loader's; the structures a
background build publishes into are guarded by ``loader._catalog_lock``.
"""

from __future__ import annotations

import fnmatch
import functools
import glob as glob_module
import hashlib
import json
import logging
import os
import threading
import time
from contextvars import copy_context
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader, _ScopedSkillEntry

logger = logging.getLogger("kiro_crew.skills")


def _disabled_app_names() -> frozenset[str]:
    """Installed apps that are currently DISABLED.

    Keeps a disabled app's bundled skills out of trigger matching.
    ``bridges`` registers each app skill under ``skills/<app>/<skill>`` (plus a
    flat link), so the first path segment names the owning app.

    Read once per matching pass rather than per skill: this runs on every
    message, and ``is_app_enabled`` reads a JSON file per call. Failures return
    an EMPTY set on purpose — the gate then hides nothing, which keeps a
    transient read error from silently stripping an enabled app's skills.
    Deferred import: ``apps.manager`` is a higher layer than this module.
    """
    try:
        from kiro_crew.apps.manager import list_apps

        return frozenset(
            str(a.get("name")) for a in list_apps() if a.get("name") and not a.get("enabled")
        )
    except Exception:
        logger.debug("skills: could not read app enablement", exc_info=True)
        return frozenset()


@functools.lru_cache(maxsize=None)
def _builtin_dir_app_name(pkg_dir: str) -> str | None:
    """The manifest name of the builtin app shipped in *pkg_dir*, or ``None``.

    A shipped builtin's package directory is named for its Python package
    (``auto_improvement``) while the app registry keys on the manifest name
    (``auto-improvement``), so the mapping must come from the manifest itself —
    the same source ``apps.discovery`` registers builtins from. Cached for the
    process lifetime: the installed package tree is immutable while running,
    and this is consulted from the per-message trigger-matching pass.
    """
    try:
        with open(os.path.join(pkg_dir, "app.json"), encoding="utf-8") as fh:
            name = json.load(fh).get("name")
        return name if isinstance(name, str) and name else None
    except Exception:
        return None


_GLOB_CHARS = "*?["


def _literal_split(pattern: str) -> tuple[tuple[str, ...], int]:
    """*pattern*'s path parts and how many lead it before the first wildcard."""
    parts = Path(pattern).parts
    literal = 0
    while literal < len(parts) and not any(char in parts[literal] for char in _GLOB_CHARS):
        literal += 1
    return parts, literal


def _canonical_prefix(pattern: str, project_dir: str | Path | None = None) -> str | None:
    """The literal (wildcard-free) prefix of *pattern* resolved through symlinks.

    ``skills.extra_paths`` and edition roots are stored resolved, but
    ``expand_skill_uri`` expands ``~`` from ``$HOME`` as spelled, so a
    symlinked home names the same file through two paths.

    The prefix is resolved through ``validate_file_path``, which applies the
    Windows UNC gate before any resolution and refuses sensitive paths: a
    mapping glob comes from an agent spec, possibly a project's, so resolving
    it directly could open an SMB connection to a host the spec names. A
    prefix inside *project_dir* is never resolved, because project paths are
    walked by descriptor and not canonicalized before consent. Returns None
    for a refused, project-local or wildcard-first pattern.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    parts, literal = _literal_split(pattern)
    if literal == 0:
        return None
    literal_prefix = str(Path(*parts[:literal]))
    try:
        if project_dir and sk._within_any(
            os.path.abspath(literal_prefix), (os.path.abspath(project_dir),)
        ):
            return None
        return sk.validate_file_path(literal_prefix)
    except (OSError, ValueError):
        return None


def _project_prefix(pattern: str, project_dir: str | Path | None, project_key: str) -> str | None:
    """*pattern*'s literal prefix respelled under the trusted *project_key*.

    A workspace-relative mapping expands against *project_dir* as spelled,
    while the project tier is enumerated under its canonical trust key, so a
    project reached through a symlink names the same row through two paths.
    The swap is lexical: *project_key* is already the resolved form of
    *project_dir* and is set only once the project is trusted, so no project
    path is resolved here. Returns None outside a trusted project.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not project_dir or not project_key:
        return None
    parts, literal = _literal_split(pattern)
    if literal == 0:
        return None
    literal_prefix = os.path.abspath(str(Path(*parts[:literal])))
    spelled = os.path.abspath(project_dir)
    if not sk._within_any(literal_prefix, (spelled,)):
        return None
    return os.path.normpath(os.path.join(project_key, os.path.relpath(literal_prefix, spelled)))


def _glob_with_prefix(pattern: str, head: str | None) -> str:
    """*pattern* with its literal prefix replaced by the resolved *head*.

    Only the literal prefix changes; the wildcard tail is kept as written so
    it still matches every entry it did before. *head* is glob-escaped, so a
    link target whose name holds ``*``, ``?`` or ``[`` matches only itself.
    A None *head* leaves *pattern* as written.
    """
    if head is None:
        return pattern
    parts, literal = _literal_split(pattern)
    return str(Path(glob_module.escape(head), *parts[literal:]))


def _canonical_glob(pattern: str, project_dir: str | Path | None = None) -> str:
    """*pattern* with its literal prefix resolved by :func:`_canonical_prefix`."""
    return _glob_with_prefix(pattern, _canonical_prefix(pattern, project_dir))


def _with_canonical_globs(globs: list[str], project_dir: str | Path | None = None) -> list[str]:
    """*globs* plus each one's canonical spelling, original order first, no duplicates."""
    out = list(globs)
    for glob in globs:
        canonical = _canonical_glob(glob, project_dir)
        if canonical not in out:
            out.append(canonical)
    return out


def _matches_any(path: str, globs: list[str]) -> bool:
    """True if *path* matches any fnmatch glob in *globs*.

    Narrows the injected skills block to an agent template's
    ``skill://`` mapping. A symlinked skill dir is tried in resolved form too
    so a mapping written against the link target still matches the catalog's
    listed path. Callers comparing against resolved catalog paths pass the
    globs through :func:`_with_canonical_globs` so a glob spelled through a
    symlink (a symlinked ``$HOME``) matches as well.
    """
    if not path:
        return False
    if any(fnmatch.fnmatch(path, g) for g in globs):
        return True
    try:
        real = str(Path(path).resolve(strict=True))
    except OSError:
        return False
    return real != path and any(fnmatch.fnmatch(real, g) for g in globs)


def _iter(
    loader: SkillsLoader, project_dir: str | Path | None = None
) -> list[tuple[str, Path, str | None]]:
    """Return all ``(name, skill_file, within)`` triples without walking the tree.

    Local skills take precedence over extra paths, and both take precedence
    over a trusted project's own skills. Precedence is why enumeration ORDER is
    part of the answer and not an incidental detail of how it was produced.

    Three tiers, none of which walks on the calling thread:

    1. this loader's in-memory list, while it is inside
       ``_ITER_CACHE_TTL_SECS``;
    2. the same list past that deadline. Expiry SCHEDULES a re-walk; it never
       charges one to the caller that happened to arrive after it;
    3. the stored snapshot for this root set, adopted into tier 1. A restart,
       and every short-lived loader (the unsigned MCP fallback builds one per
       call), lands here rather than walking.

    Only a scope with no snapshot at all — a machine's first run, or one whose
    index file was deleted — waits, and then for at most
    ``_COLD_CATALOG_WAIT_SECS`` on a background build. Past that the partial
    answer is served with the scope marked incomplete, so a caller can say
    "still discovering" instead of "no skills"; see :meth:`catalog_status`.

    A served list says only which files EXIST. Mapping scope, disabled apps,
    project consent and ``repo_scope`` are all applied live on top of it, and
    every body read re-checks confinement on the descriptor it opened — so no
    snapshot, however stale, can widen what a session reaches.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    key = loader._catalog_scope_key(project_dir)
    now = time.monotonic()
    with loader._catalog_lock:
        cached = loader._iter_cache.get(key)
        stale = cached is not None and now >= cached[0]
    if cached is not None:
        if stale:
            loader._request_catalog_refresh(key)
        return cached[1]

    snapshot = loader._load_catalog_snapshot(key)
    if snapshot is not None:
        rows, built_at = snapshot
        loader._adopt_catalog(key, rows, {}, complete=True)
        if time.time() - built_at >= sk._CATALOG_REVALIDATE_AFTER_SECS:
            loader._request_catalog_refresh(key)
        return rows

    with loader._catalog_lock:
        already_reported = key in loader._catalog_incomplete
    if already_reported:
        # "Still discovering" has already been delivered for this scope, so
        # charging every later turn the same budget would buy nothing and break
        # the rule that a subsequent turn never waits.
        loader._request_catalog_refresh(key)
        logger.debug("skill catalog: scope %r still building", key or "<global>")
        return []

    # Waiting on the WORKER rather than walking here is what bounds the cost:
    # the walk continues after the budget expires, so the wait buys a complete
    # answer when one is cheap and costs a fixed ceiling when it is not. The
    # loop is what makes the budget the only limit: a build can be FENCED by a
    # mutation that lands while it runs and then publishes nothing, and its
    # replacement is queued behind it — so waking on one build's completion is
    # not the same as the answer being ready.
    deadline = time.monotonic() + sk._COLD_CATALOG_WAIT_SECS
    while True:
        done = loader._request_catalog_refresh(key)
        remaining = deadline - time.monotonic()
        if done is None or remaining <= 0:
            break
        done.wait(timeout=remaining)
        with loader._catalog_lock:
            cached = loader._iter_cache.get(key)
        if cached is not None:
            return cached[1]
    with loader._catalog_lock:
        cached = loader._iter_cache.get(key)
        if cached is not None:
            return cached[1]
        # The build is still running. Serve nothing, but RECORD that this is a
        # partial answer so no caller reports it as a complete "no skills".
        loader._catalog_incomplete.add(key)
    logger.debug("skill catalog: first build of scope %r still running", key or "<global>")
    return []


def _catalog_scope_key(loader: SkillsLoader, project_dir: str | Path | None) -> str:
    """The scope this request reads, as a string the snapshot layer can key on.

    ``_trusted_project_key`` answers ``""`` for "no trusted project", but a
    falsy answer of any shape means the same thing, and the two must not select
    DIFFERENT scopes — one would then be served a snapshot the other built. So
    the coercion happens once, here, rather than at each of the three call
    sites that would otherwise each have to remember it.
    """
    return loader._trusted_project_key(project_dir) or ""


def catalog_status(loader: SkillsLoader, project_dir: str | Path | None = None) -> str:
    """``"complete"`` or ``"building"`` for the scope *project_dir* selects.

    The distinction a caller cannot make from an empty result alone: a machine
    with no skills and a machine whose first discovery pass has not finished
    both enumerate to nothing. Search, list and directory callers use this to
    say which one it is rather than presenting a partial answer as the truth.
    """
    key = loader._catalog_scope_key(project_dir)
    with loader._catalog_lock:
        return "building" if key in loader._catalog_incomplete else "complete"


def _catalog_fingerprint_hint(
    loader: SkillsLoader, project_dir: str | Path | None
) -> dict[str, str]:
    """Stat fingerprints from the walk that produced this scope's list.

    Empty when this process has not walked the scope yet, which simply sends
    ``list_skills`` down its own stat path.
    """
    key = loader._catalog_scope_key(project_dir)
    with loader._catalog_lock:
        return loader._catalog_fingerprints.get(key, {})


def _catalog_scope_id(loader: SkillsLoader, project_key: str) -> str:
    """Stable identity of the ROOT SET a stored snapshot belongs to.

    The project key alone is not enough: two loaders can share it and still
    enumerate different trees (a different skills dir under a test
    ``KIROCREW_HOME``, a different ``skills.extra_paths``). Keying on the roots
    as well is what stops one configuration from being served another's
    snapshot. It is also what makes a revoked project grant unreachable rather
    than merely unused: withdrawing trust turns the project key back into ``""``
    (see ``_trusted_project_key``), which selects a DIFFERENT scope, so the rows
    naming that project's files cannot be read from. Digested rather than
    concatenated so the key stays bounded and holds no path text for an
    unrelated reader of the index file.
    """
    material = "\x00".join(
        [str(loader._dir), *(str(path) for path in loader._extra_paths), project_key]
    )
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _snapshot_admitted_roots(loader: SkillsLoader) -> tuple[str, ...]:
    """Roots an unconfined row read off disk may legitimately name.

    The walk admits an unconfined path one of two ways: it came out of a walk
    of this loader's own roots, or ``validate_file_path`` resolved it — which
    may land on an app provider's tree, since an app symlinks its skills into
    the skills dir and the resolved target sits outside it by construction. A
    row read back from the index has neither guarantee, so it is held to the
    union of both: this loader's roots plus the same provider roots the mapping
    walker admits. Lexical, so screening a whole snapshot costs no syscalls.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    return (
        os.path.realpath(loader._dir),
        *(os.path.realpath(path) for path in loader._extra_paths),
        *sk._trusted_skill_roots(),
    )


def _load_catalog_snapshot(
    loader: SkillsLoader, project_key: str
) -> tuple[list[tuple[str, Path, str | None]], float] | None:
    """Read this scope's stored enumeration, or ``None`` when there is none.

    ``None`` and an empty row list are different answers: the second means this
    root set was walked and genuinely holds no skills, which must not be
    re-walked on every message just because the answer is nothing.

    No stat fingerprints come back with it, deliberately. They would record what
    a walk in some earlier process saw, and nothing here knows how long ago that
    was, so letting them stand in for metadata validation would make a new
    process serve a description for a file edited out of band since — the one
    thing a restart is expected to notice. A stored row may name a file; it may
    never vouch for its contents.

    The index file is an agent-WRITABLE crew-home leaf, so a stored row is not
    evidence that anything admitted the path it names. Two things therefore
    stand between a row and a read. Here, every unconfined row must name a path
    under :meth:`_snapshot_admitted_roots` — lexical, so a whole snapshot is
    screened without a syscall — and rows that fail are dropped rather than
    served. Then each surviving unconfined path is recorded as NOT YET ADMITTED,
    because containment alone does not say the file is still a regular file in a
    non-sensitive location; ``_read_enumerated_skill_bytes`` re-runs
    ``validate_file_path`` on it before the first read.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if loader._search_index is None or loader._closed:
        return None
    stored = loader._search_index.catalog_snapshot(loader._catalog_scope_id(project_key))
    if stored is None:
        return None
    rows_raw, built_at = stored
    admitted_roots = loader._snapshot_admitted_roots()
    own_roots = (loader._dir, *loader._extra_paths)
    provider_roots = sk._trusted_skill_roots()
    rows: list[tuple[str, Path, str | None]] = []
    unadmitted: set[str] = set()
    for key, path, confine_root in rows_raw:
        absolute = os.path.abspath(path)
        if confine_root:
            # A confined row's root decides which directory its body is read
            # under, so taking the stored value on trust would let a forged row
            # name ANY project and have it read as a granted one. Only the
            # trusted project key that selected this scope is accepted, the path
            # must sit inside it, and the key must denote that path — the same
            # pair check the unconfined branch applies, for the same reason: the
            # mapping is matched on the path and the body delivered by the key.
            if (
                not project_key
                or confine_root != project_key
                or not sk._within_any(absolute, (project_key,))
                or os.path.abspath(Path(project_key) / ".kiro" / "skills" / key / sk._SKILL_FILE)
                != absolute
            ):
                logger.warning("skill catalog: refusing a stored row with a foreign root")
                return None
            rows.append((key, Path(path), confine_root))
            continue
        if not sk._within_any(absolute, admitted_roots):
            logger.warning("skill catalog: refusing a stored row outside every root")
            return None
        if not loader._key_denotes_path(key, absolute, own_roots, provider_roots):
            logger.warning("skill catalog: refusing a stored row whose key is not its path")
            return None
        rows.append((key, Path(path), None))
        unadmitted.add(str(Path(path)))
    with loader._catalog_lock:
        loader._snapshot_unadmitted |= unadmitted
    return rows, built_at


def _admit_snapshot_path(loader: SkillsLoader, path: Path) -> bool:
    """Re-run the walk's admission on an unconfined path read off disk.

    ``True`` — and free — for every path this process walked itself, which is
    the normal case: the set only ever holds rows adopted from the stored
    snapshot, and a walk that republishes the scope empties it. A path in the
    set is put through ``validate_file_path`` exactly once; admitting it retires
    it from the set so later reads cost nothing, and a refusal leaves it in so a
    second attempt is refused again rather than silently admitted.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    key = str(path)
    with loader._catalog_lock:
        if key not in loader._snapshot_unadmitted:
            return True
    if sk.validate_file_path(key) is None:
        logger.warning("Refusing a stored skill path that no longer admits: %s", path)
        return False
    with loader._catalog_lock:
        loader._snapshot_unadmitted.discard(key)
    return True


def _key_denotes_path(
    key: str, absolute: str, own_roots: tuple[Path, ...], provider_roots: tuple[str, ...]
) -> bool:
    """Does *key* name the skill that *absolute* holds?

    A stored row carries the key and the path as two independent fields, and they
    are consumed by DIFFERENT gates: an agent mapping is matched against the
    PATH (``_matches_any``), while the body is delivered by re-resolving the KEY
    (``load_skill``). A row that pairs one skill's key with another's path
    therefore passes a mapping admitting the second and serves the first — so the
    pair has to be checked, not just each half.

    Two shapes a walk can legitimately produce, both decided lexically:

    * the path IS what the key denotes under one of this loader's own roots, which
      is what the global tree and an in-root ``extra_paths`` entry record;
    * the path is an admitted PROVIDER target — an app symlinks its skills into
      the tree, so ``validate_file_path`` resolves the row out of the root it was
      named in — and the key's last segment still matches the directory holding
      the file.

    Anything else is refused, and the caller refuses the whole snapshot with it: a
    dropped row would leave a truncated catalog being served as a complete one.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not key:
        return False
    for root in own_roots:
        if os.path.abspath(root / key / sk._SKILL_FILE) == absolute:
            return True
    return sk._within_any(absolute, provider_roots) and (
        os.path.basename(os.path.dirname(absolute)) == key.rsplit("/", 1)[-1]
    )


def _adopt_catalog(
    loader: SkillsLoader,
    project_key: str,
    rows: list[tuple[str, Path, str | None]],
    fingerprints: dict[str, str],
    *,
    complete: bool,
) -> None:
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    with loader._catalog_lock:
        loader._iter_cache[project_key] = (time.monotonic() + sk._ITER_CACHE_TTL_SECS, rows)
        if fingerprints:
            loader._catalog_fingerprints[project_key] = fingerprints
        if complete:
            loader._catalog_incomplete.discard(project_key)


def _request_catalog_refresh(loader: SkillsLoader, project_key: str) -> threading.Event | None:
    """Queue one background walk of *project_key*'s roots; join any in flight.

    Returns the event that build sets on completion, so the cold path can wait
    on it with a budget. ``None`` means no build could be started — a closed
    loader, or a host that refused the thread — and the caller must then live
    with what it already has rather than walking on the request path.
    """
    with loader._catalog_lock:
        if loader._closed:
            return None
        existing = loader._catalog_refreshes.get(project_key)
        if existing is not None and not existing[0].is_set():
            if existing[1] != loader._catalog_generation:
                # The build in flight was queued before a mutation, so it is
                # already fenced and will publish nothing. Queue the current
                # generation behind it, or the caller waits out its budget on an
                # answer that never lands and the next turn pays cold discovery.
                loader._catalog_pending[project_key] = loader._catalog_generation
                loader._catalog_wakeup.set()
            return existing[0]
        done = threading.Event()
        loader._catalog_refreshes[project_key] = (done, loader._catalog_generation)
        loader._catalog_pending[project_key] = loader._catalog_generation
        if loader._catalog_worker is None or not loader._catalog_worker.is_alive():
            # Started on first NEED, not in __init__: a loader whose every call
            # is served from a snapshot never starts a thread, which is what
            # keeps the short-lived MCP fallback cheap. DAEMON because a build
            # is deliberately abandonable, and `concurrent.futures` joins its
            # non-daemon workers at interpreter exit — which would make a walk
            # this design moved off the request path delay process exit instead
            # of being dropped.
            try:
                worker = threading.Thread(
                    target=copy_context().run,
                    args=(loader._catalog_worker_loop,),
                    name="skill-catalog-refresh",
                    daemon=True,
                )
                worker.start()
            except RuntimeError:  # pragma: no cover — host refused a thread
                logger.debug("skill catalog: refresh worker unavailable", exc_info=True)
                loader._catalog_refreshes.pop(project_key, None)
                loader._catalog_pending.pop(project_key, None)
                return None
            loader._catalog_worker = worker
    loader._catalog_wakeup.set()
    return done


def _catalog_worker_loop(loader: SkillsLoader) -> None:
    """Drain queued scopes one at a time until this loader closes.

    Serial by construction: several sessions in different trusted projects all
    enumerate the same global tree, so running their builds concurrently would
    multiply that one corpus's filesystem work by the session count.
    """
    while True:
        with loader._catalog_lock:
            if loader._closed:
                return
            if loader._catalog_pending:
                project_key, generation = loader._catalog_pending.popitem()
            else:
                # Cleared under the same lock the producer sets it under, and
                # only after observing an empty queue, so a scope queued in
                # between cannot lose its wakeup.
                loader._catalog_wakeup.clear()
                project_key, generation = "", -1
        if generation < 0:
            loader._catalog_wakeup.wait()
            continue
        loader._run_catalog_build(project_key, generation)


def _run_catalog_build(loader: SkillsLoader, project_key: str, generation: int) -> None:
    """Walk *project_key*'s roots off the request path and publish the result.

    Failure is logged and dropped: a scope keeps serving whatever it had, which
    is strictly better than failing a turn over a tree that could not be read
    this once.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    try:
        with loader._catalog_lock:
            if loader._closed:
                return
            # The handle is READ under the same lock that claims it. Reading it
            # first and claiming second leaves a gap in which `close()` sees no
            # build in flight, closes the index, and latches it unusable — the
            # walk then finishes and persists nothing, which is the one thing the
            # handover exists to prevent.
            index = loader._search_index
            loader._catalog_building = True
        # Both captured BEFORE the walk. The epoch is the cross-process half of
        # the generation check: `store_catalog` refuses a write whose epoch has
        # moved. The scope is captured because `skills.extra_paths` can be
        # reconfigured while the walk runs, and a scope id computed afterwards
        # would file rows walked over the OLD root set under the NEW root set's
        # key — publishing a catalog that names neither configuration's tree.
        epoch = index.catalog_epoch() if index is not None else None
        scope_id = loader._catalog_scope_id(project_key)
        rows = loader._iter_uncached(project_key or None)
        fingerprints = loader._catalog_fingerprints_for(rows)
        # PERSIST BEFORE PUBLISHING. The store is where a cross-process
        # invalidation is detected: `"stale"` means another process recorded a
        # mutation while this walk ran, so these rows must not be served either.
        # `"unavailable"` is the opposite case — the rows are fine and only the
        # database is missing (a read-only home), and refusing to serve them
        # would make every turn re-walk.
        outcome = (
            index.store_catalog(
                scope_id,
                [(name, str(path), within or "") for name, path, within in rows],
                epoch=epoch,
            )
            if index is not None
            else "unavailable"
        )
        if outcome == "stale":
            logger.debug("skill catalog: another process invalidated mid-walk; dropping")
            return
        with loader._catalog_lock:
            if loader._closed or generation != loader._catalog_generation:
                # A mutation landed while this walk ran, so its answer predates
                # a change already known. Publishing it would resurrect the
                # pre-change list and undo the invalidation. The rows stay
                # PERSISTED when only `_closed` stopped the publish: they
                # describe the tree this walk saw, and a host served only by
                # short-lived loaders converges on nothing else.
                return
            if scope_id != loader._catalog_scope_id(project_key):
                logger.debug("skill catalog: root set changed mid-walk; dropping the result")
                return
            loader._iter_cache[project_key] = (time.monotonic() + sk._ITER_CACHE_TTL_SECS, rows)
            loader._catalog_fingerprints[project_key] = fingerprints
            loader._catalog_incomplete.discard(project_key)
            # Only the paths THIS walk returned are admitted. A forged row the
            # walk rejected keeps its marker, so a reader still holding the old
            # list cannot read it unadmitted.
            loader._snapshot_unadmitted -= {
                str(path) for _name, path, within in rows if within is None
            }
    except Exception:  # noqa: BLE001 — a failed walk must not kill the worker
        logger.warning("skill catalog: background walk failed", exc_info=True)
    finally:
        with loader._catalog_lock:
            loader._catalog_building = False
            entry = loader._catalog_refreshes.pop(project_key, None)
            # The handle was held for this build; a close that arrived while it
            # ran deferred shutting it down to here.
            index_to_close = loader._search_index if loader._closed else None
        if entry is not None:
            entry[0].set()
        if index_to_close is not None:
            index_to_close.close()


def _catalog_fingerprints_for(
    rows: list[tuple[str, Path, str | None]],
) -> dict[str, str]:
    """Stat each unconfined row once, so ``list_skills`` need not stat again.

    Confined project rows are deliberately absent: their cache token comes from
    bytes the no-link reader admitted, never from a path stat, and recording one
    here would reintroduce the probe that confinement exists to prevent.
    """
    fingerprints: dict[str, str] = {}
    for _name, path, within in rows:
        if within is not None:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        fingerprints[str(path)] = (
            f"{st.st_dev}:{st.st_ino}:{st.st_ctime_ns}:{st.st_mtime_ns}:{st.st_size}"
        )
    return fingerprints


def _get_disabled_app_names(loader: SkillsLoader) -> frozenset[str]:
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    now = time.monotonic()
    if loader._disabled_apps_cache is not None and now < loader._disabled_apps_cache[0]:
        return loader._disabled_apps_cache[1]
    disabled = _disabled_app_names()
    loader._disabled_apps_cache = (now + sk._ITER_CACHE_TTL_SECS, disabled)
    return disabled


def _iter_visible(
    loader: SkillsLoader, project_dir: str | Path | None = None
) -> list[tuple[str, Path, str | None]]:
    """Return all ``(name, skill_file, within)`` pairs, filtering out disabled app skills."""
    disabled_apps = loader._get_disabled_app_names()
    if not disabled_apps:
        return loader._iter(project_dir)
    return [
        (name, skill_file, within)
        for name, skill_file, within in loader._iter(project_dir)
        if loader._owning_app(name, skill_file) not in disabled_apps
    ]


def _scoped_entries(
    loader: SkillsLoader,
    project_dir: str | Path | None,
    only: list[str] | None,
) -> list[_ScopedSkillEntry]:
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if only == []:
        return []
    entries = [sk._ScopedSkillEntry(*entry) for entry in loader._iter_visible(project_dir)]
    if only is None:
        return entries
    project_key = loader._trusted_project_key(project_dir) if project_dir else ""
    heads = {
        pattern: _canonical_prefix(pattern, project_dir)
        or _project_prefix(pattern, project_dir, project_key)
        for pattern in only
    }
    canonicals = {pattern: _glob_with_prefix(pattern, heads[pattern]) for pattern in only}
    globs = list(dict.fromkeys([*only, *canonicals.values()]))
    selected = [entry for entry in entries if _matches_any(str(entry[1]), globs)]
    known = {os.path.normcase(os.path.abspath(entry[1])) for entry in entries}
    loader_roots = [loader._dir, *loader._extra_paths]
    catalog_roots = list(loader_roots)
    if project_dir:
        catalog_roots.append(Path(project_dir) / ".kiro" / "skills")
    # Compared both as spelled and resolved: ``skills.extra_paths`` roots
    # are stored resolved while a ``~`` mapping may name them through a
    # symlink. Only loader-owned roots are resolved, through the same
    # UNC-gated fence as the glob prefix; the project root stays lexical
    # because a project path is never canonicalized before consent.
    real_roots = [real for root in loader_roots if (real := sk.validate_file_path(str(root)))]
    # An ancestor glob must not descend through a catalog's filtered
    # rows or probe a project skills junction before consent admission.
    excluded = tuple(
        dict.fromkeys([*(os.path.abspath(path) for path in catalog_roots), *real_roots])
    )
    # Mapping paths are spec-owned, never caller-supplied read keys. Walk the
    # literal prefix through the same provider/sensitive-path fence as the
    # global catalog; do not use glob's unconstrained link traversal.
    for pattern in only:
        canonical = canonicals[pattern]
        prefix = Path(pattern)
        while any(char in str(prefix) for char in _GLOB_CHARS):
            prefix = prefix.parent
        head = heads[pattern]
        real_prefix = Path(head) if head is not None else prefix
        if any(
            sk._within_any(os.path.abspath(prefix), (os.path.abspath(root),))
            for root in catalog_roots
        ) or sk._within_any(str(real_prefix), tuple(real_roots)):
            # The regular enumerator owns precedence, disabled apps and
            # project consent. A mapping must not re-admit a filtered row.
            continue
        root = prefix.parent if prefix.name == "SKILL.md" else prefix
        if not root.is_absolute() or sk.validate_file_path(str(root)) is None:
            continue
        admitted_roots = (os.path.realpath(root), *sk._trusted_skill_roots())
        for _name, path in sk._iter_skill_files(root, exclude_roots=excluded):
            identity = os.path.normcase(os.path.abspath(path))
            if identity in known or not _matches_any(str(path), [pattern, canonical]):
                continue
            target = os.path.realpath(path)
            admitted_root = next(
                (candidate for candidate in admitted_roots if sk._within_any(target, (candidate,))),
                None,
            )
            if admitted_root is None:
                continue
            known.add(identity)
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            selected.append(
                sk._ScopedSkillEntry(
                    f"mapped/{digest}/{path.parent.name}", path, None, admitted_root
                )
            )
    return selected


def _iter_uncached(
    loader: SkillsLoader, project_key: str | None = None
) -> list[tuple[str, Path, str | None]]:
    """Walk the skills dir, extra paths, and an already-canonical project root.

    This function performs no trust check of its own. The loading path passes
    a key confirmed by ``_trusted_project_key``; the catalog path uses it only
    to determine which confined names a later grant could admit. Callers must
    never pass a raw caller-supplied path.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    # Unconfined (None): the global tree may legitimately hold app-registered
    # symlinks resolving into a provider root outside it.
    results: list[tuple[str, Path, str | None]] = [
        (name, path, None) for name, path in sk._iter_skill_files(loader._dir)
    ]
    seen = {name for name, _, _ in results}
    # (root, confine_to): only the project root is confined — see
    # _iter_skill_files. Extra paths keep the provider-root allowance.
    roots: list[tuple[Path, tuple[str, ...] | None]] = [
        (extra, None) for extra in loader._extra_paths
    ]
    if project_key:
        project_root = Path(project_key) / ".kiro" / "skills"
        # Appended LAST so a repository cannot shadow a same-named skill
        # the operator installed globally. The confined walker opens the
        # project root and every descendant component relative to no-follow
        # directory handles; no path probe occurs before that confinement.
        roots.append((project_root, (project_key,)))
    for root, confine in roots:
        for name, skill_file in sk._iter_skill_files(root, confine_to=confine):
            if name in seen:
                continue
            if confine is not None:
                # The descriptor-anchored walker already admitted this
                # lexical name. Resolving it here would reintroduce the
                # link-swap/UNC probe the walk exists to prevent. Reads are
                # re-confined at their own descriptor-pinned choke point.
                results.append((name, skill_file, confine[0]))
                seen.add(name)
                continue
            # Route through hooks validation (resolves symlinks + sensitive
            # check) so files read later during trigger matching are vetted.
            resolved = sk.validate_file_path(str(skill_file))
            if resolved is None:
                continue
            # The vetted root travels WITH the item. Containment is only
            # knowable here, and a side map keyed on the path string kept
            # going wrong: the key could disagree with the value handed out,
            # and a miss read unconfined. Carried in the tuple, neither is
            # expressible.
            results.append((name, Path(resolved), None))
            seen.add(name)
    return results


def _invalidate_iter_cache(loader: SkillsLoader) -> None:
    """Drop cached skill state so a just-written mutation is visible now.

    Called by create/update/delete/refresh. Clears both the skill-file list
    cache AND the mtime-keyed frontmatter cache: an in-place ``update_skill``
    can overwrite a file within the same filesystem mtime tick as the prior
    read, so keying the frontmatter cache on mtime alone would return the
    stale parse. Dropping it here keeps the mutator's edit immediately
    reflected in ``list_skills`` / ``get_triggered_skills``.

    The stored snapshot goes too, and both generation counters move. Each is
    required for a different race: leaving the snapshot would let the next
    process serve the pre-mutation list, and leaving the generations alone would
    let a walk already in flight publish its pre-mutation answer on top of this
    clear — an invalidation a background refresh silently undoes. The in-process
    counter covers this loader's own worker; the index's epoch, which
    ``drop_catalog`` bumps, covers a walk running in another process.

    Emptying the list rather than serving the pre-mutation one is what makes the
    mutator's edit visible immediately, and it is also why the re-walk is QUEUED
    here instead of waiting for the next turn to demand it: on a tree whose walk
    outlasts ``_COLD_CATALOG_WAIT_SECS`` the next turn would otherwise re-enter
    the cold path, and starting the walk now bounds that window to the walk's own
    duration.
    """
    loader._disabled_apps_cache = None
    with loader._catalog_lock:
        scopes = list(loader._iter_cache)
        loader._iter_cache = {}
        loader._catalog_fingerprints = {}
        loader._catalog_incomplete.clear()
        loader._snapshot_unadmitted.clear()
        loader._catalog_generation += 1
        index = loader._search_index
    loader._fm_cache.clear()
    if index is not None:
        index.drop_catalog()
    for scope in scopes:
        loader._request_catalog_refresh(scope)


def _owning_app(loader: SkillsLoader, name: str, skill_file: Path) -> str | None:
    """The app whose bundle this skill came from, or ``None``.

    Two shapes have to resolve to the same owner, because ``bridges``
    registers every app skill twice and either registration can be the one
    this walk kept (see ``_iter_skill_files``'s ``seen_real`` note):

    * the namespaced ``skills/<app>/<skill>`` directory — the first segment
      of ``name`` IS the app;
    * the flat ``skills/<skill>`` link, whose name says nothing — so the
      real path is consulted. An externally installed app resolves under
      the data home's apps root, where the directory name IS the app name.
      A shipped BUILTIN resolves inside the package tree
      (``…/apps/builtins/<pkg dir>/skills/…``), and its package directory
      (``auto_improvement``) is not its app name (``auto-improvement``) —
      the manifest in that directory is the authoritative mapping (see
      ``apps.discovery``).

    Path-shaped, not manifest-keyed, on purpose: it must answer for a
    third-party app just as well as a builtin, and the registration layout
    is the one thing every app shares.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    head = name.split("/", 1)[0]
    if head != name:
        return head
    try:
        from kiro_crew.apps.manager import apps_dir

        real = Path(os.path.realpath(skill_file))
        root = apps_dir()
        if real.is_relative_to(root):
            # <apps root>/<app>/... — the segment directly under the root.
            return real.relative_to(root).parts[0]
        builtins_root = Path(os.path.realpath(Path(sk.__file__).parent)) / "apps" / "builtins"
        if real.is_relative_to(builtins_root):
            pkg_dir = builtins_root / real.relative_to(builtins_root).parts[0]
            return _builtin_dir_app_name(str(pkg_dir))
    except Exception:
        return None
    return None


def _resolve_path(
    loader: SkillsLoader, name: str, project_dir: str | Path | None = None
) -> Path | None:
    """Return the ``SKILL.md`` path for an enumerated skill *name*.

    Allowlist-only, like ``resolve_dollar_skills``: the path comes from the
    enumeration rather than being constructed from *name*, so a crafted
    name cannot escape the skill roots.

    Prefer :meth:`_resolve_path_and_root` when the path will be READ — the
    root a path is confined to is decided by the enumeration, and a caller
    that only has the path would have to guess it.
    """
    resolved = loader._resolve_path_and_root(name, project_dir)
    return resolved[0] if resolved else None


def _resolve_path_and_root(
    loader: SkillsLoader, name: str, project_dir: str | Path | None = None
) -> tuple[Path, str | None] | None:
    """The enumerated path for *name* PLUS the root it is confined to.

    The enumeration is the only place containment is knowable, so it is also
    the only place that may answer this. Handing both back together is what
    stops a reader from inventing a root, or from reading with none.
    """
    for candidate, skill_file, within in loader._iter(project_dir):
        if candidate == name:
            return skill_file, within
    return None
