"""Named memory stores: the two roots, the shape rule, and the resolvers.

A crew (``cfg.agents[<crew>].memory_store``) names a memory store, and a store
is a SEPARATE on-disk silo: its own markdown tree, its own FTS index and its
own vector-store SQLite file. There is no workspace column and no cutover — isolation is the
file boundary.

**There are TWO roots, and conflating them is the sharpest hazard here.** The
resolvers below answer for the same store name, and they answer with different
paths:

* :func:`memory_store_dir_for` — the MARKDOWN root, the directory holding
  ``memory/preferences.md``, ``memory/projects.md`` and ``memory/history/*.md``.
  For ``"default"`` this is :func:`kiro_crew.memory.workspace_dir`
  (``config_dir()/"workspace"``), NOT the data home: returning the data home
  would move every existing install's markdown memory out from under both the
  consolidator and ``kirocrew memory search``.
* :func:`resolve_store_path` — the VECTOR FILE. For ``"default"`` this is
  ``config_dir()/"memory.db"``, byte-identical to the path
  ``VectorMemoryStore()`` already defaults to.

:func:`memory_index_path_for` answers a third question and does NOT follow the
markdown root: the DEFAULT store's FTS index stays in the data-home root, beside
``memory.db``, because that is the location the snapshot ``memory`` component,
``portability``'s export zip and ``scripts/sync-to-remote.sh`` name for it. A
NAMED store's index does live inside its own directory, and the first two of
those consumers carry the whole ``memory_stores/`` tree (minus the host-local
entries :func:`is_host_local_store_state` names).

Nothing here moves data. A named store starts EMPTY; nothing is copied or
inferred from the default store.

A supplied named store is resolved exactly or raises UnknownMemoryStore. Existing
members retain their declared V1 binding until the owner selects private V2.
New members receive an empty owned V2 store. Private ownership is recorded in
config, the bounded member-memory.json manifest and the database.

LEAF module: stdlib-only imports at module scope, so ``security.py`` (imported
very early, and which needs :data:`MEMORY_STORES_DIR_NAME` to build its
sensitive-path fence) can depend on it without a cycle. Everything else is
imported inside the function that needs it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

logger = logging.getLogger(__name__)

#: Directory under the data home holding one subdirectory per NAMED store. The
#: default store is deliberately NOT under here — it keeps the pre-existing
#: ``workspace/`` tree and root ``memory.db``.
#:
#: Read+write fenced: this whole subtree is a keystone leaf in
#: ``security._CREW_SECRET_LEAVES``, so agent file tools can neither read nor
#: write another crew's memory. Legitimate readers open these paths DIRECTLY,
#: the established keystone-reader pattern.
MEMORY_STORES_DIR_NAME = "memory_stores"

#: The store name that is always resolvable. It is the FLOOR, in the sense
#: ``ACP_BACKEND_KIRO`` is the harness floor: it names the markdown tree and
#: vector file every existing install already has, so it counts as declared
#: whether or not the operator's ``memory_stores`` section mentions it. That is
#: what keeps a fresh install (no ``config.json`` at all) resolvable.
DEFAULT_MEMORY_STORE = "default"

#: Vector-store filename inside a store's own directory. Owned here because two
#: resolvers must spell it identically — this module's
#: :func:`resolve_store_path` and ``vector_memory``'s own default.
MEMORY_DB_FILE = "memory.db"

#: Entries under ``memory_stores/`` that are THIS HOST's runtime state rather than
#: anyone's memory, spelled here so the writers and the backup tools agree on them.
#: A bundle (``kirocrew snapshot``, the dashboard export) never carries them in
#: either direction: the signing key is regenerated on the restoring host exactly
#: as ``sel_hmac.key`` is, the execution logs are per-process diagnostics of runs
#: that happened here, and the backup directories are the local rolling-durability
#: copies plus their pending-restore journals -- the default store's own
#: ``<home>/backups/`` is outside every snapshot component for the same reason.
MEMBER_API_KEY_FILE = ".member-api-key"
MEMBER_BACKUPS_DIR_NAME = ".member-backups"
EXECUTION_LOGS_DIR_NAME = ".execution-logs"
#: Local retirement decisions survive restore and cannot be supplied by an archive.
MEMBER_MEMORY_ARCHIVE_DIR = ".archived-members"
#: A NAMED V1 store's rolling backups sit inside its own directory (``memory_backup``
#: aliases this); a V2 member's sit under :data:`MEMBER_BACKUPS_DIR_NAME` instead.
STORE_BACKUP_DIR_NAME = "backups"
_HOST_LOCAL_ROOT_ENTRIES: frozenset[str] = frozenset(
    {
        MEMBER_API_KEY_FILE,
        MEMBER_BACKUPS_DIR_NAME,
        EXECUTION_LOGS_DIR_NAME,
        MEMBER_MEMORY_ARCHIVE_DIR,
    }
)

#: Longest usable store name. A store name becomes a single path segment, and a
#: 255-byte filesystem limit has to hold the name plus whatever a sidecar
#: appends to it, so the cap is well inside it rather than at it.
MEMORY_STORE_NAME_MAX = 80

# Same shape ``members._SLUG_RE`` enforces for member slugs — lowercase
# letters, digits and hyphens, no leading or trailing hyphen. Kept as a local
# constant rather than imported because it is private there, and because the
# two lists are allowed to diverge; the members store remains the source of
# truth for the spelling.
_STORE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

# Basenames Windows resolves to a DEVICE rather than a file, with or without an
# extension. Refused on every platform, not just Windows: a config written on
# Linux is carried to Windows, and a store whose directory cannot be created
# there is a silo that silently holds nothing.
_WINDOWS_RESERVED_BASENAMES: frozenset[str] = frozenset(
    {"con", "nul", "aux", "prn"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


class UnknownMemoryStore(ValueError):
    """A requested store cannot be used without losing its identity or ownership."""


class MemberAlreadyExists(UnknownMemoryStore):
    """A member creation lost a race with an existing config entry."""


def memory_store_name_defect(name: object) -> str | None:
    """Why *name* is unusable as a store name, or ``None`` when it is fine.

    The predicate half of :func:`validate_memory_store_name`, also used when
    reporting invalid config declarations without discarding the original data.

    Rules are ordered most-specific-first so the reported reason names the
    actual defect; :data:`_STORE_NAME_RE` is the final catch-all.
    """
    if not isinstance(name, str):
        return "not a string"
    if not name:
        return "empty"
    if len(name) > MEMORY_STORE_NAME_MAX:
        return f"longer than {MEMORY_STORE_NAME_MAX} characters"
    if name != name.lower():
        return "not lowercase"
    # A SINGLE path segment, checked against BOTH separators: ``a\\b`` is one
    # segment to ``posixpath`` and two to Windows, and the config is portable.
    if os.path.basename(name) != name or Path(name).name != name or "\\" in name:
        return "not a single path segment"
    if name in _WINDOWS_RESERVED_BASENAMES:
        return "a Windows reserved device basename"
    if name[-1] in ". ":
        return "ends with a dot or a space"
    if not _STORE_NAME_RE.match(name):
        return f"does not match {_STORE_NAME_RE.pattern}"
    return None


def memory_store_binding_defect(raw: object) -> str | None:
    """Why a submitted binding's shape is unusable, or ``None``.

    Empty strings remain accepted at the write boundary for old clients. New
    members are provisioned automatically; existing bindings are immutable and
    require ownership validation independently of this shape check. A malformed
    binding does not authorize global memory at runtime.
    """
    if isinstance(raw, str) and not raw:
        return None
    return memory_store_name_defect(raw)


def named_store_or_empty(name: object) -> str:
    """*name* as a NAMED store, or ``""`` meaning the global store.

    The one definition of "this value means the global store". Six call sites
    across five modules spelled it themselves and two of them had already
    diverged: one stripped surrounding whitespace and one did not, so a
    hand-edited ``"  coding  "`` made the consolidator WRITE into the silo while
    the context builder READ the global store — a split-brain with no error on
    either side.

    Strips deliberately. A padded value cannot pass
    :func:`validate_memory_store_name` (a trailing space is a refusal, because a
    path segment ending in one is unusable on Windows), so the choice is between
    stripping here and answering two different things in two modules. Every
    write surface already rejects padding; this is what makes a config the
    validators never saw resolve the same way everywhere.

    Non-strings answer ``""``: metadata is read from disk and a caller must not
    have to type-check before asking.
    """
    if not isinstance(name, str):
        return ""
    stripped = name.strip()
    return "" if not stripped or stripped == DEFAULT_MEMORY_STORE else stripped


def validate_memory_store_name(name: str) -> str:
    """Return *name* unchanged when it is a usable store name, else raise.

    Applied before path composition, including when resolving config bindings.
    The pattern admits no ``/``, ``\\``, ``.`` or whitespace, so a validated name
    cannot traverse out of :func:`memory_stores_root` on its own; the resolvers
    still re-check containment after composition, because validation and use
    are separated by a call boundary a future caller could bypass (the same
    pairing ``members.validate_slug`` / ``members.member_dir`` uses).
    """
    defect = memory_store_name_defect(name)
    if defect is not None:
        raise UnknownMemoryStore(f"invalid memory store name {name!r}: {defect}")
    return name


def memory_stores_root() -> Path:
    """The directory holding one subdirectory per NAMED memory store."""
    from kiro_crew.config.loader import config_dir

    return config_dir() / MEMORY_STORES_DIR_NAME


def usable_store_names(declared: Iterable[str]) -> frozenset[str]:
    """The subset of *declared* that can actually become a store on disk.

    A MALFORMED name is undeclared for resolution even though
    ``KiroCrewConfig.load`` keeps the operator's entry verbatim — reporting a
    defect must not erase a line the operator wrote, and a name no resolver will
    compose a path for is still not a store any crew can run on. Both membership
    tests in the tree run through this filter (:func:`_declared_stores` here,
    ``config.loader.resolve_agent_bindings`` for a crew's binding), which is what
    keeps them from disagreeing: a raw-table test would hand a crew a name that
    :func:`validate_memory_store_name` then refuses at the first memory write.

    Filtering ONLY — :data:`DEFAULT_MEMORY_STORE` is not added here. The floor
    belongs to the resolvers, which must answer for it on an install with no
    ``config.json`` at all; a crew's binding reads a loaded table that already
    carries a synthesized default entry whenever the section was empty, so
    adding the floor there would instead change which store an existing config's
    crew lands on.

    Pure — no config load — so ``resolve_agent_bindings`` can call it with the
    config already in hand instead of re-entering the loader.
    """
    return frozenset(n for n in declared if memory_store_name_defect(n) is None)


#: ``(config fingerprint, declared names, configured default)`` for the last
#: resolution. Not an LRU: there is exactly one config, so one slot is the whole
#: cache, and keying on the fingerprint means a stale entry is impossible rather
#: than merely unlikely.
_DECLARED_MEMO: tuple[object, frozenset[str], str] | None = None


def _set_declared_memo(fp: object, declared: frozenset[str], configured_default: str) -> None:
    global _DECLARED_MEMO
    _DECLARED_MEMO = (fp, declared, configured_default)


def _declared_stores() -> tuple[frozenset[str], str]:
    """``(resolvable store names, cfg.default_memory_store)`` off the LOADED config.

    Reads ``KiroCrewConfig.load()``, never ``_raw_config()``. The raw dict is
    the bytes on disk: it carries no ``memory_stores`` key until a write-back
    migration adds one, and that migration is SKIPPED whenever the load
    degraded a section — so a raw-dict resolver reports ``"default"`` as
    undeclared on a fresh install, and keeps reporting it on any install with a
    malformed config section. The loaded config synthesizes the default entry.

    :data:`DEFAULT_MEMORY_STORE` is unioned in unconditionally: it is the floor,
    naming the markdown tree and vector file every install already has, so it
    stays resolvable even when the config cannot be read at all.

    Never raises — a config that will not load degrades to the floor alone.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig, _config_fingerprint

        # Memoized on the loader's OWN change-detector, so it invalidates exactly
        # when the config does. A full ``KiroCrewConfig.load()`` deep-copies the
        # cached dict and rebuilds every dataclass to answer for two fields, and
        # resolution runs several times per turn on a named store; keying on the
        # fingerprint turns that into a stat.
        fp = _config_fingerprint()
        memo = _DECLARED_MEMO
        if memo is not None and memo[0] == fp:
            return memo[1], memo[2]
        cfg = KiroCrewConfig.load()
        declared = usable_store_names(cfg.memory_stores) | {DEFAULT_MEMORY_STORE}
        _set_declared_memo(fp, declared, cfg.default_memory_store)
        return declared, cfg.default_memory_store
    except Exception:
        logger.warning(
            "could not load config to enumerate memory stores; using the %r store only",
            DEFAULT_MEMORY_STORE,
            exc_info=True,
        )
        return frozenset({DEFAULT_MEMORY_STORE}), DEFAULT_MEMORY_STORE


def resolve_declared_store(store: str) -> str:
    """Resolve the exact declared store; a supplied name never falls back to V1."""
    validate_memory_store_name(store)
    if store == DEFAULT_MEMORY_STORE:
        return store
    declared, _ = _declared_stores()
    if store not in declared:
        raise UnknownMemoryStore(
            f"memory store {store!r} is not declared; global memory was not used"
        )
    return store


def _named_store_dir(name: str) -> Path:
    """Compose a NAMED store's directory and re-check containment.

    *name* must already be shape-validated and must not be
    :data:`DEFAULT_MEMORY_STORE`.
    """
    root = memory_stores_root().resolve()
    expected = root / name
    target = expected.resolve()
    # Defence in depth behind validate_memory_store_name, mirroring
    # members.member_dir: a symlinked component must not redirect a store.
    #
    # The test is IDENTITY, not containment, and the difference is a real
    # isolation hole rather than a hypothetical one. Checking only
    # ``target.parent == root`` refuses a link that escapes the root and ACCEPTS
    # one that redirects INSIDE it: with ``memory_stores/acme`` pointing at
    # ``memory_stores/finance``, the resolved parent is still the root, so both
    # crews were handed one silo -- vector rows, markdown and lessons -- with
    # every path check reporting success. Requiring the resolved path to be the
    # one that was composed refuses the redirect and still admits a root reached
    # through a symlinked ancestor (``/tmp`` on macOS), because ``root`` is
    # resolved before the join.
    #
    # A non-existent store resolves to itself (``strict=False``), so a store
    # being created for the first time passes.
    if target != expected:
        raise UnknownMemoryStore(
            f"memory store {name!r} resolves to {target}, not {expected}; refusing a "
            f"link that would share another store's directory"
        )
    return target


def memory_store_dir_for(store: str) -> Path:
    """The MARKDOWN root for *store*. Does not create anything.

    ``"default"`` resolves to ``memory.workspace_dir()``
    (``config_dir()/"workspace"``) so no existing install's ``preferences.md``,
    ``projects.md`` or ``history/`` moves. A declared name resolves to
    ``config_dir()/memory_stores/<name>``.

    NOT the home of the FTS index for every store — the default store's index
    sits in the data-home root instead. :func:`memory_index_path_for` owns that.
    """
    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.memory import workspace_dir

        return workspace_dir()
    return _named_store_dir(name)


def memory_index_path_for(store: str) -> Path:
    """The FTS5 index file for *store*. Does not create anything.

    ``"default"`` resolves to ``config_dir()/memory_index.db``, the data-home
    root — where the index of every existing install already sits, and the place
    the off-store consumers look for it: the snapshot ``memory`` component's
    ``files`` tuple, ``portability``'s export/import zip and
    ``scripts/sync-to-remote.sh`` all name it root-relative. So this is
    deliberately NOT ``memory_store_dir_for(store)``'s answer for the default
    store; moving it there would silently drop the index from every backup while
    a restore wrote a copy nothing reads.

    A NAMED store's index lives inside that store's own directory, beside the
    markdown tree it describes, which is what makes the index per-store and puts
    it behind the ``memory_stores/`` fence. The snapshot and the export carry
    that directory as part of the ``memory_stores/`` tree, so the index rides
    beside its markdown there; ``sync-to-remote.sh`` still names root paths only.

    The index is fully DERIVED — ``MemoryStore.rebuild_index`` regenerates it
    from preferences.md, projects.md and history/*.md and reads no index state —
    so a store whose index is not backed up loses search results until the next
    rebuild, never memory.
    """
    from kiro_crew.memory import INDEX_DB_FILE

    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.config.loader import config_dir

        return config_dir() / INDEX_DB_FILE
    return _named_store_dir(name) / INDEX_DB_FILE


def resolve_store_path(store: str) -> Path:
    """The VECTOR FILE (semantic/episodic/lessons SQLite) for *store*.

    ``"default"`` resolves to ``config_dir()/"memory.db"`` — byte-exact with
    ``VectorMemoryStore()``'s own default, so the default store keeps the file
    it already has. A named store gets ``memory.db`` inside its own directory,
    which is also what scopes ``VectorMemoryStore.init``'s owner-only
    tightening of ``db_path.parent`` to that store.
    """
    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.config.loader import config_dir

        return config_dir() / MEMORY_DB_FILE
    return _named_store_dir(name) / MEMORY_DB_FILE


def named_store_of_db(path: Path) -> str:
    """The NAMED store whose vector file is *path*, or ``""`` when it is not one.

    The inverse of :func:`resolve_store_path`, and the only POSITIVE spelling of
    "this file is a crew silo". Answers ``""`` for the default store, for an eval
    or import destination, for a bare temp path, and for anything malformed —
    which is the whole point. The negation a caller would otherwise reach for,
    ``path != config_dir()/"memory.db"``, is true of four real non-silo paths
    (the eval runner's ``ws/"vector_memory.db"``, the bench ingest path, the
    onboarding importer's ``destination/"memory.db"``, and every ``tmp_path`` in
    the suite), so it would hand each of them silo treatment.

    The containment test is IDENTITY, for the reason spelled out in
    :func:`_named_store_dir`: with ``memory_stores/acme`` symlinked at
    ``memory_stores/finance``, a resolved-parent check still sees the root and
    would answer ``"acme"`` for a file that physically belongs to ``finance`` —
    naming the alias rather than the store, which is the same aliasing hole the
    forward direction already refuses.
    """
    if path.name != MEMORY_DB_FILE:
        return ""
    parent = path.parent
    name = parent.name
    # ``named_store_or_empty`` rather than a bare shape check, so the literal
    # ``memory_stores/default/`` answers "" here too. That directory is
    # unreachable through ``resolve_store_path`` (which maps the name to the
    # data-home root before composing a path), but a caller handing this function
    # an arbitrary path must not be told the name of the GLOBAL store.
    if named_store_or_empty(name) != name or memory_store_name_defect(name) is not None:
        return ""
    try:
        root = memory_stores_root().resolve()
        if parent.resolve() != root / name:
            return ""
    except OSError:
        return ""
    return name


def is_host_local_store_state(rel_parts: Sequence[str]) -> bool:
    """Is the DATA-HOME-relative path *rel_parts* host-local state under ``memory_stores/``?

    The one spelling of what a bundle leaves out of the ``memory_stores/`` tree, shared
    by the snapshot's staging walk, its extraction filter and the dashboard export, so
    the three cannot disagree about what "the memory" is. True for the direct children
    listed at :data:`MEMBER_API_KEY_FILE` and its siblings, and for a named store's own
    :data:`STORE_BACKUP_DIR_NAME`. Everything else under the tree -- the markdown, the
    vector file, the index, ``lessons.jsonl``, the ownership manifest -- IS the memory
    and rides.

    Purely lexical, never a filesystem call: callers ask about archive members and
    unverified directory listings, where resolving a name is the probe they exist to
    avoid.
    """
    if len(rel_parts) < 2 or rel_parts[0] != MEMORY_STORES_DIR_NAME:
        return False
    if rel_parts[1] in _HOST_LOCAL_ROOT_ENTRIES:
        return True
    return len(rel_parts) >= 3 and rel_parts[2] == STORE_BACKUP_DIR_NAME


def named_store_product_file(rel_parts: Sequence[str]) -> str:
    """The product database *rel_parts* (data-home-relative) names inside a store, or ``""``.

    Answers :data:`MEMORY_DB_FILE` for ``memory_stores/<name>/memory.db`` and the FTS
    index filename for ``memory_stores/<name>/memory_index.db``, and ``""`` for anything
    else -- a file deeper in the tree, a malformed store name, or the unreachable
    ``memory_stores/default/`` spelling. The name check is :func:`named_store_or_empty`
    plus :func:`memory_store_name_defect`, the same pair :func:`named_store_of_db`
    applies, so a path this says is ours is one the resolvers could actually hand out.

    What the answer buys: the backup tools validate a product database as strictly as
    the root ``memory.db`` and treat a derived index as rebuildable, and neither can be
    keyed on a fixed path because store names are the operator's. Lexical only, for
    the same reason as :func:`is_host_local_store_state`.
    """
    if len(rel_parts) != 3 or rel_parts[0] != MEMORY_STORES_DIR_NAME:
        return ""
    name = rel_parts[1]
    if named_store_or_empty(name) != name or memory_store_name_defect(name) is not None:
        return ""
    # circular import: this module is a stdlib-only leaf (see the module docstring) and
    # ``memory`` reaches ``security``, which imports this module at import time.
    from kiro_crew.memory import INDEX_DB_FILE

    return rel_parts[2] if rel_parts[2] in (MEMORY_DB_FILE, INDEX_DB_FILE) else ""


def declared_store_names() -> list[str]:
    """Every store name a whole-install pass covers: the DEFAULT store first, then the rest.

    ONE enumeration, because a pass that builds its own is a pass that can disagree with
    another about which stores exist -- and a store missing from one of them is a store
    whose contents that pass reports nothing about while still printing a verdict.

    Names come off the operator's DECLARED table through :func:`usable_store_names`, the
    one filter every membership test runs through. A directory listing of
    ``memory_stores/`` is deliberately NOT used: it would adopt a silo the config no
    longer declares, or one a restore dropped in, and then treat it as the operator's.

    DEFAULT FIRST, then sorted -- not sorted overall. The default store is the one every
    install has, so it leads every report; a plain sort buries it wherever the alphabet
    puts it and makes two passes over the same install list in different orders.

    Never raises. A config that cannot be read degrades to the default store alone, the
    same floor :func:`_declared_stores` falls back to.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        declared = usable_store_names(KiroCrewConfig.load().memory_stores)
    except Exception:
        logger.warning(
            "could not enumerate declared memory stores; using %r alone",
            DEFAULT_MEMORY_STORE,
            exc_info=True,
        )
        return [DEFAULT_MEMORY_STORE]
    return [DEFAULT_MEMORY_STORE, *sorted(declared - {DEFAULT_MEMORY_STORE})]


def active_store_names() -> list[str]:
    """Routine maintenance targets; archived private stores remain owner-visible."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        config = KiroCrewConfig.load()
    except Exception:
        logger.warning("could not enumerate active memory stores", exc_info=True)
        return [DEFAULT_MEMORY_STORE]
    bindings: dict[str, list[str]] = {}
    for member, agent in config.agents.items():
        if isinstance(agent.memory_store, str):
            bindings.setdefault(agent.memory_store, []).append(member)
    active = []
    for name in sorted(usable_store_names(config.memory_stores) - {DEFAULT_MEMORY_STORE}):
        record = config.memory_stores[name]
        owner = getattr(record, "owner_member", "")
        if owner or getattr(record, "memory_version", 1) == 2:
            if (
                not owner
                or getattr(record, "memory_version", 1) != 2
                or bindings.get(name) != [owner]
            ):
                continue
        active.append(name)
    return [DEFAULT_MEMORY_STORE, *active]


def owned_store_path(store: str) -> Path | None:
    """*store*'s vector file, or ``None`` when the resolution does not belong to it.

    Whole-install maintenance can skip an unavailable store while continuing
    with the others. Runtime member resolution uses the raising ownership
    validators instead; this helper must never choose a replacement store.
    """
    try:
        path = resolve_store_path(store)
    except Exception:
        logger.warning("memory store %r has no resolvable vector file", store, exc_info=True)
        return None
    if named_store_or_empty(store) and named_store_of_db(path) != store:
        logger.warning(
            "memory store %r resolved to %s, which is not that store's own file", store, path
        )
        return None
    return path


def ensure_memory_store_dir(store: str) -> Path:
    """Create *store*'s markdown root owner-only and return it.

    The stores ROOT is created and tightened before its first child exists,
    which is what the Windows half depends on: ``restrict_dir_to_owner``'s
    grants carry ``(OI)(CI)``, so a store directory created inside an
    already-tightened root inherits owner-only access instead of landing on the
    creating token's default DACL.

    ``"default"`` is returned untouched: its root is the pre-existing
    ``workspace/`` tree, created and owned by ``MemoryStore.init()``, and
    creating or tightening it from here would change the default path.
    """
    from kiro_crew.memory_startup import require_memory_ready

    name = resolve_declared_store(store)
    require_memory_ready(name)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.memory import workspace_dir

        return workspace_dir()
    from kiro_crew import platform_compat

    with memory_store_namespace_lock():
        platform_compat.make_owner_only_dir(memory_stores_root())
        target = _named_store_dir(name)
        platform_compat.make_owner_only_dir(target)
        return target


MEMBER_MEMORY_MANIFEST = "member-memory.json"
_MEMBER_MEMORY_ARCHIVE_FILE = "archive.json"


def _member_archive_path(name: str) -> Path:
    """Stable retirement marker outside the store tree a restore can replace."""
    validated = validate_memory_store_name(name)
    return (
        memory_stores_root().resolve()
        / MEMBER_MEMORY_ARCHIVE_DIR
        / validated
        / _MEMBER_MEMORY_ARCHIVE_FILE
    )


def _member_archive_record(name: str) -> dict | None:
    """Read one durable retirement marker, refusing malformed committed state."""
    from kiro_crew.session_pid_sig import _read_regular_nofollow

    try:
        # Resolve the supported home/root alias before composing the protected
        # archive path, then reject redirects even when the final leaf is absent.
        path = _member_archive_path(name)
        directory = path.parent
        if directory.resolve() != directory or path.resolve() != path:
            raise OSError("retirement marker is redirected")
        try:
            directory.lstat()
        except FileNotFoundError:
            return None
        raw = _read_regular_nofollow(path)
        if raw is None:
            raise OSError("retirement marker is unreadable")
        if len(raw.encode("utf-8")) > 4096:
            raise ValueError("retirement marker is too large")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or value.get("archived") is not True
            or value.get("memory_store") != name
            or not isinstance(value.get("owner_member"), str)
            or not value["owner_member"]
        ):
            raise ValueError("retirement marker has invalid identity")
        return value
    except (OSError, ValueError) as exc:
        raise UnknownMemoryStore(
            f"memory store {name!r} retirement marker is invalid: {exc}"
        ) from exc


def require_member_memory_not_archived(name: str, *, expected_owner: str = "") -> None:
    """Refuse a retired V2 generation even if raw config binds it again."""
    record = _member_archive_record(name)
    if record is None:
        return
    if expected_owner and record["owner_member"] != expected_owner:
        raise UnknownMemoryStore(
            f"memory store {name!r} retirement owner does not match {expected_owner!r}"
        )
    raise UnknownMemoryStore(
        f"memory store {name!r} is archived; retained data was not reactivated"
    )


def _publish_member_archive_dir(staging: Path, destination: Path) -> None:
    """Publish a complete retirement directory without replacing a winner."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:
        os.rename(staging, destination)
        return
    parent_fd = os.open(staging.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        platform_compat.rename_noreplace(
            staging.name,
            destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)


def archive_member_memory_store(name: str, expected_owner: str) -> bool:
    """Durably retire one exact private generation before removing its member.

    Returns true only when this call created the marker, so a failed config
    write can roll back its own admission change without removing an older one.
    """
    from kiro_crew import platform_compat
    from kiro_crew.atomic_write import fsync_dir

    existing = _member_archive_record(name)
    if existing is not None:
        if existing["owner_member"] != expected_owner:
            raise UnknownMemoryStore(f"memory store {name!r} retirement identity changed")
        return False
    owner, version = member_memory_identity(name)
    if owner != expected_owner or version != 2:
        raise UnknownMemoryStore(
            f"memory store {name!r} does not belong to Crew Member {expected_owner!r}"
        )
    path = _member_archive_path(name)
    archive_root = path.parent.parent
    platform_compat.make_owner_only_dir(memory_stores_root())
    platform_compat.make_owner_only_dir(archive_root)
    payload = json.dumps(
        {
            "version": 1,
            "archived": True,
            "memory_store": name,
            "owner_member": expected_owner,
        },
        sort_keys=True,
    )
    staging = Path(
        tempfile.mkdtemp(prefix=f".{path.parent.name}.", suffix=".tmp", dir=archive_root)
    )
    try:
        platform_compat.restrict_dir_to_owner(staging)
        staged_path = staging / _MEMBER_MEMORY_ARCHIVE_FILE
        descriptor = os.open(
            staged_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        platform_compat.restrict_to_owner(staged_path)  # lockdown-ok: owner-only staging
        fsync_dir(staging)
        published = False
        try:
            _publish_member_archive_dir(staging, path.parent)
            published = True
        except FileExistsError:
            record = _member_archive_record(name)
            if record is None or record["owner_member"] != expected_owner:
                raise UnknownMemoryStore(f"memory store {name!r} retirement identity changed")
            return False
        try:
            fsync_dir(archive_root)
        except BaseException:
            if published:
                # Publication is visible but its directory entry was not made
                # durable. Retract it atomically while the config lock still
                # proves the member is live, rather than strand an active owner
                # behind a marker whose caller was told creation failed.
                rollback_member_memory_archive(name, expected_owner)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return True


def rollback_member_memory_archive(name: str, expected_owner: str) -> None:
    """Remove only a marker this failed deletion transaction just created."""
    from kiro_crew.atomic_write import fsync_dir

    record = _member_archive_record(name)
    if record is None or record["owner_member"] != expected_owner:
        raise UnknownMemoryStore(f"memory store {name!r} retirement identity changed")
    path = _member_archive_path(name)
    archive_root = path.parent.parent
    retired = archive_root / f".{path.parent.name}.rollback-{uuid.uuid4().hex}"
    os.rename(path.parent, retired)
    fsync_dir(archive_root)
    (retired / path.name).unlink()
    retired.rmdir()
    fsync_dir(archive_root)


def rollback_member_memory_archive_if_active(name: str, expected_owner: str) -> bool:
    """Rollback only while config still binds the same live member generation."""
    from kiro_crew.config.loader import coerce_dict_section, update_config_locked

    rolled_back = False

    def inspect(data: dict) -> None:
        nonlocal rolled_back
        agents = coerce_dict_section(data, "agents")
        current = agents.get(expected_owner)
        if isinstance(current, dict) and current.get("memory_store") == name:
            rollback_member_memory_archive(name, expected_owner)
            rolled_back = True
        return None

    with memory_store_namespace_lock():
        update_config_locked(mutate=inspect, stamp_meta=False)
    return rolled_back


def _member_manifest(name: str) -> dict:
    """Read a bounded ownership record without following a substituted file."""
    target = _named_store_dir(validate_memory_store_name(name))
    manifest = target / MEMBER_MEMORY_MANIFEST
    try:
        if manifest.resolve() != manifest or manifest.stat().st_size > 4096:
            raise UnknownMemoryStore(f"memory store {name!r} has an invalid ownership record")
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("ownership record must be an object")
        return value
    except (OSError, ValueError) as exc:
        raise UnknownMemoryStore(
            f"memory store {name!r} ownership record is missing or unreadable: {exc}"
        ) from exc


def member_memory_identity(store: str) -> tuple[str, int]:
    """Return a validated private manifest identity, or fail closed."""
    require_member_memory_not_archived(store)
    value = _member_manifest(store)
    owner = value.get("owner_member")
    version = value.get("memory_version")
    if not isinstance(owner, str) or not owner or owner == DEFAULT_MEMORY_STORE or version != 2:
        raise UnknownMemoryStore(
            f"memory store {store!r} ownership record is not a private V2 identity"
        )
    return owner, version


def memory_store_version(store: str) -> int:
    """Identify V2 positively; the global store and unowned legacy stores are V1.

    Reads only the bounded ownership manifest, so it is safe during vector-store
    initialization and does not recursively load config. Runtime authorization
    must still use ``require_member_memory_store``.
    """
    if store == DEFAULT_MEMORY_STORE or memory_store_name_defect(store) is not None:
        return 1
    try:
        member_memory_identity(store)
    except UnknownMemoryStore:
        return 1
    return 2


def _require_legacy_store_files(store: str, target: Path) -> None:
    """A legacy declaration cannot erase private manifest or database evidence."""
    import sqlite3
    import stat

    from kiro_crew.memory_schema import OWNER_MEMBER_META_KEY, PRIVATE_MEMORY_VERSION_META_KEY

    manifest = target / MEMBER_MEMORY_MANIFEST
    try:
        try:
            manifest.lstat()
        except FileNotFoundError:
            pass
        else:
            raise UnknownMemoryStore(
                f"memory store {store!r} retains private ownership evidence; V1 was not used"
            )
        database = target / MEMORY_DB_FILE
        try:
            info = database.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("database is not an exclusive regular file")
        if database.resolve() != database:
            raise OSError("database is redirected")
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type IN ('table', 'view') AND name='memory_meta'"
            ).fetchone()
            if (
                has_meta
                and connection.execute(
                    "SELECT 1 FROM memory_meta WHERE key IN (?, ?) LIMIT 1",
                    (PRIVATE_MEMORY_VERSION_META_KEY, OWNER_MEMBER_META_KEY),
                ).fetchone()
            ):
                raise UnknownMemoryStore(
                    f"memory store {store!r} retains private database identity; V1 was not used"
                )
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise UnknownMemoryStore(
            f"memory store {store!r} legacy identity cannot be verified: {exc}"
        ) from exc


def _private_database_owner_hint(store: str, member: str) -> bool:
    """Read only the ownership row as refusal evidence, never as admission."""
    import sqlite3
    import stat

    from kiro_crew.memory_schema import OWNER_MEMBER_META_KEY

    try:
        database = _named_store_dir(store) / MEMORY_DB_FILE
        if database.resolve() != database:
            return False
        descriptor = os.open(
            database, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return False
            if os.read(descriptor, 16) != b"SQLite format 3\x00":
                return False
        finally:
            os.close(descriptor)
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT 1 FROM memory_meta WHERE key=? AND value=? LIMIT 1",
                (OWNER_MEMBER_META_KEY, member),
            ).fetchone()
            return row is not None
        finally:
            connection.close()
    except (OSError, sqlite3.Error, UnknownMemoryStore):
        return False


def _require_legacy_member_binding(config, member: str, *, inspect_files: bool) -> None:
    """Reject a lost private binding without adopting any discovered store."""
    for name, record in config.memory_stores.items():
        if getattr(record, "owner_member", "") == member:
            archived = _member_archive_record(name) if inspect_files else None
            if archived is None or archived["owner_member"] != member:
                raise UnknownMemoryStore(
                    f"Crew Member {member!r} has a private memory declaration; "
                    "its missing or changed binding cannot use V1"
                )
    if not inspect_files:
        return
    root = memory_stores_root().resolve()
    slug = re.sub(r"[^a-z0-9]+", "-", member.lower()).strip("-")[:32] or "crew"
    prefix = f"member-{slug}-"
    try:
        with os.scandir(root) as entries:
            names = [entry.name for entry in entries]
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UnknownMemoryStore(
            f"Crew Member {member!r} private memory evidence is unreadable: {exc}"
        ) from exc
    for name in names:
        if name == DEFAULT_MEMORY_STORE or memory_store_name_defect(name) is not None:
            continue
        suffix = name.removeprefix(prefix)
        generated = name.startswith(prefix) and re.fullmatch(r"[0-9a-f]{32}", suffix) is not None
        try:
            manifest = _member_manifest(name)
        except UnknownMemoryStore:
            # The generated name is a refusal hint only. It never grants an
            # owner, adopts a store, or makes an unrelated corrupt peer fatal.
            manifest = {"owner_member": member} if generated else {}
        if not manifest.get("owner_member") and not generated:
            manifest = {
                "owner_member": member if _private_database_owner_hint(name, member) else ""
            }
        if generated and (
            not isinstance(manifest.get("owner_member"), str)
            or not manifest["owner_member"]
            or manifest.get("memory_version") != 2
        ):
            manifest = {"owner_member": member}
        if manifest.get("owner_member") != member:
            continue
        archived = _member_archive_record(name)
        if archived is not None and archived["owner_member"] == member:
            continue
        raise UnknownMemoryStore(
            f"Crew Member {member!r} retains private memory evidence at {name!r}; "
            "restore its binding instead of using V1"
        )


def require_memory_store(store: str, *, config=None, require_directory: bool = True) -> str:
    """Validate a trusted persisted store binding, without fallback or repair.

    ``default`` is the explicit V1 identity. Callers must distinguish absent
    legacy metadata from malformed or missing member bindings before calling.
    Directory existence is required on use, so deleting a member directory
    cannot silently replace its memory with an empty store.
    """
    validate_memory_store_name(store)
    if store == DEFAULT_MEMORY_STORE:
        if require_directory:
            from kiro_crew.memory_startup import require_memory_ready

            require_memory_ready(store)
        return store
    # A retained store tree and ownership manifest are evidence of the retired
    # generation, not authority to reactivate it. Keep this on the shared
    # admission seam so callers that intentionally skip filesystem readiness
    # (configuration and scheduling validation) cannot bypass retirement.
    require_member_memory_not_archived(store)
    if require_directory:
        from kiro_crew.memory_startup import require_memory_ready

        require_memory_ready(store)
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    if store not in usable_store_names(config.memory_stores):
        raise UnknownMemoryStore(
            f"memory store {store!r} is not declared; global memory was not used"
        )
    if require_directory:
        target = _named_store_dir(store)
        try:
            # scandir actually opens the directory, unlike exists()/os.access().
            with os.scandir(target):
                pass
        except OSError as exc:
            raise UnknownMemoryStore(
                f"memory store {store!r} is missing or unreadable: {exc}; global memory was not used"
            ) from exc
    record = config.memory_stores[store]
    owner = getattr(record, "owner_member", "")
    if type(getattr(record, "memory_version", None)) is not int:
        raise UnknownMemoryStore(f"memory store {store!r} has an invalid memory version")
    if owner or getattr(record, "memory_version", 1) == 2:
        if not isinstance(owner, str) or not owner or owner == DEFAULT_MEMORY_STORE:
            raise UnknownMemoryStore(f"memory store {store!r} has no valid private owner")
        if getattr(record, "memory_version", 1) != 2:
            raise UnknownMemoryStore(f"memory store {store!r} has inconsistent memory version")
        bindings = [name for name, agent in config.agents.items() if agent.memory_store == store]
        if bindings != [owner]:
            raise UnknownMemoryStore(
                f"memory store {store!r} must belong exclusively to member {owner!r}"
            )
        if require_directory:
            manifest = _member_manifest(store)
            if manifest.get("owner_member") != owner or manifest.get("memory_version") != 2:
                raise UnknownMemoryStore(
                    f"memory store {store!r} ownership does not match member {owner!r}"
                )
            database = target / MEMORY_DB_FILE
            try:
                if database.resolve() != database:
                    raise OSError("database is redirected to another file")
                with database.open("rb") as handle:
                    if os.fstat(handle.fileno()).st_nlink != 1:
                        raise OSError("database has a hard-link alias")
                    if handle.read(16) != b"SQLite format 3\x00":
                        raise OSError("database header is invalid")
            except OSError as exc:
                raise UnknownMemoryStore(
                    f"memory store {store!r} database is missing or unreadable: {exc}"
                ) from exc
    else:
        if owner != "" or record.memory_version != 1:
            raise UnknownMemoryStore(f"memory store {store!r} has an invalid legacy declaration")
        if require_directory:
            _require_legacy_store_files(store, target)
    return store


def require_member_memory_store(config, member: str, *, require_directory: bool = True) -> str:
    """Resolve an existing V1 binding or an exclusively owned V2 store exactly."""
    if member == DEFAULT_MEMORY_STORE:
        if require_directory:
            from kiro_crew.memory_startup import require_memory_ready

            require_memory_ready(DEFAULT_MEMORY_STORE)
        return DEFAULT_MEMORY_STORE
    agent = config.agents.get(member)
    if agent is None:
        raise UnknownMemoryStore(f"unknown Crew Member {member!r}; global memory was not used")
    store = agent.memory_store
    validate_memory_store_name(store)
    record = config.memory_stores.get(store) if isinstance(store, str) else None
    if store == DEFAULT_MEMORY_STORE or (
        record is not None
        and getattr(record, "owner_member", None) == ""
        and type(getattr(record, "memory_version", None)) is int
        and record.memory_version == 1
    ):
        _require_legacy_member_binding(config, member, inspect_files=require_directory)
        return require_memory_store(store, config=config, require_directory=require_directory)
    if (
        not isinstance(store, str)
        or store == DEFAULT_MEMORY_STORE
        or record is None
        or getattr(record, "owner_member", "") != member
        or getattr(record, "memory_version", 1) != 2
    ):
        raise UnknownMemoryStore(
            f"Crew Member {member!r} has a missing or invalid memory binding. "
            "Existing memory was not changed."
        )
    return require_memory_store(store, config=config, require_directory=require_directory)


_NAMESPACE_LOCK_STATE = threading.local()


def named_store_operation(method):
    """Hold replacement admission for a named store operation on every platform."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        if not self._memory_store_name:
            return method(self, *args, **kwargs)
        with memory_store_namespace_lock():
            return method(self, *args, **kwargs)

    return guarded


@contextmanager
def memory_store_namespace_lock(root: Path | None = None) -> Iterator[None]:
    """Serialize store allocation, publication and replacement before enumerating names.

    The lock lives in the host-local directory that replacement and rollback keep.
    Nested operations on the same thread share one hold. Operation-local file and
    configuration locks come after it; replace probes lifetime locks without waiting.
    """
    import stat

    from kiro_crew import platform_compat

    root = (root if root is not None else memory_stores_root()).resolve()
    held = getattr(_NAMESPACE_LOCK_STATE, "roots", None)
    if held is None:
        held = _NAMESPACE_LOCK_STATE.roots = set()
    if root in held:
        yield
        return
    directory = root / MEMBER_BACKUPS_DIR_NAME
    if directory.resolve() != directory:
        raise UnknownMemoryStore("memory store namespace lock directory is redirected")
    platform_compat.make_owner_only_dir(directory)
    path = directory / ".namespace.lock"
    if path.resolve() != path:
        raise UnknownMemoryStore("memory store namespace lock is redirected")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnknownMemoryStore("memory store namespace lock is not a private regular file")
        platform_compat.restrict_to_owner(path)
        with platform_compat.file_lock(fd, exclusive=True, required=True):
            held.add(root)
            try:
                yield
            finally:
                held.remove(root)
    finally:
        os.close(fd)


@memory_store_namespace_lock()
def provision_member_memory(config, member: str) -> str:
    """Allocate an empty private V2 store and bind the member in the given config.

    The caller holds its config mutation lock and persists the record before
    exposing the member. An exclusive random directory claim and ownership
    manifest prevent another process or a recreated member adopting old data.
    Existing private ownership is immutable; this is only creation/explicit
    initialization of legacy members, never a reset operation.
    """
    from kiro_crew import platform_compat
    from kiro_crew.config.sections import MemoryStoreConfig
    from kiro_crew.memory_startup import require_memory_prepared

    require_memory_prepared()

    if member == DEFAULT_MEMORY_STORE:
        raise UnknownMemoryStore("the default assistant keeps Global Memory V1")
    if member not in config.agents:
        raise UnknownMemoryStore(f"unknown Crew Member {member!r}")
    agent = config.agents[member]
    current = (
        config.memory_stores.get(agent.memory_store)
        if isinstance(agent.memory_store, str)
        else None
    )
    if current and (
        getattr(current, "owner_member", "")
        or type(current.memory_version) is not int
        or current.memory_version != 1
    ):
        return require_member_memory_store(config, member)
    # A genuinely new member may reuse an old display name while allocating a
    # fresh generation. Only an existing member is making the V1-to-V2 choice.
    from kiro_crew.config.loader import KiroCrewConfig

    if member in KiroCrewConfig.load().agents:
        _require_legacy_member_binding(config, member, inspect_files=True)
    if isinstance(agent.memory_store, str) and agent.memory_store != DEFAULT_MEMORY_STORE:
        if memory_store_name_defect(agent.memory_store) is None:
            _require_legacy_store_files(agent.memory_store, _named_store_dir(agent.memory_store))
    # A deleted or package-pruned member leaves its old ownership record and
    # directory intact.  Reusing the display name is a new member generation:
    # allocate a new random store below rather than silently adopting that
    # retained history.  The old store remains unbound and therefore fails
    # require_memory_store() until an explicit recovery flow restores it.
    root = memory_stores_root()
    platform_compat.make_owner_only_dir(root)
    slug = re.sub(r"[^a-z0-9]+", "-", member.lower()).strip("-")[:32] or "crew"
    while True:
        name = f"member-{slug}-{uuid.uuid4().hex}"
        if name in config.memory_stores:
            continue
        target = _named_store_dir(name)
        try:
            target.mkdir(mode=0o700, exist_ok=False)
            break
        except FileExistsError:
            continue
    manifest = target / MEMBER_MEMORY_MANIFEST
    try:
        platform_compat.make_owner_only_dir(target)
        with manifest.open("x", encoding="utf-8") as handle:
            json.dump({"owner_member": member, "memory_version": 2}, handle)
        # Create the canonical V2 database before the member becomes visible.
        # init() selects algorithms from the manifest without loading config.
        from kiro_crew.vector_memory import VectorMemoryStore

        vectors = VectorMemoryStore(db_path=target / MEMORY_DB_FILE)
        try:
            vectors.init()
        finally:
            vectors.close()
        config.memory_stores[name] = MemoryStoreConfig(owner_member=member, memory_version=2)
        agent.memory_store = name
    except BaseException:
        # Only the just-created manifest and an empty directory are removed.
        # Never recursively clean a path which another component may have used.
        manifest.unlink(missing_ok=True)
        try:
            target.rmdir()
        except OSError:
            logger.warning(
                "failed member initialization left an unreferenced directory at %s", target
            )
        raise
    return name


@memory_store_namespace_lock()
def retire_unpublished_member_memory_store(name: str, expected_owner: str) -> bool:
    """Retire one fresh allocation only while current config has no publication.

    The allocation has already created a complete private store, so deleting it
    would discard evidence and make an ambiguous failure look like an empty retry.
    Instead, publish the ordinary retirement marker while holding the same
    cross-process config lock used by every member binding writer.  A store
    declaration or any agent reference is enough to preserve the generation:
    either means a competing/completed publication may own it.  Malformed raw
    sections likewise refuse cleanup because absence cannot be proved safely.

    The caller must pass only the key freshly returned by its own allocation,
    never an existing V2 binding returned by provision's idempotent path.
    Returns true only when this call created the retirement marker.  The config
    document is inspected but never rewritten; a missing config is uncertainty
    and preserves the store.
    """
    from kiro_crew.config.loader import config_path, update_config_locked

    try:
        locked_path = config_path().resolve(strict=True)
    except OSError as exc:
        raise UnknownMemoryStore(
            "agent configuration is unavailable; the private allocation was preserved"
        ) from exc

    retired = False

    def retire_if_unpublished(data: dict) -> None:
        nonlocal retired
        try:
            locked_path.lstat()
        except OSError as exc:
            raise UnknownMemoryStore(
                "agent configuration is unavailable; the private allocation was preserved"
            ) from exc
        agents = data.get("agents", {})
        stores = data.get("memory_stores", {})
        if not isinstance(agents, dict) or not isinstance(stores, dict):
            raise UnknownMemoryStore(
                "agent or memory store configuration is unreadable; "
                "the private allocation was preserved"
            )
        if name in stores:
            return None
        for entry in agents.values():
            if not isinstance(entry, dict):
                raise UnknownMemoryStore(
                    "agent configuration is unreadable; the private allocation was preserved"
                )
            if entry.get("memory_store", DEFAULT_MEMORY_STORE) == name:
                return None
        retired = archive_member_memory_store(name, expected_owner)
        return None

    update_config_locked(locked_path, mutate=retire_if_unpublished)
    return retired


@memory_store_namespace_lock()
def persist_member_config(
    config,
    member: str,
    *,
    create: bool = False,
    expected_store=None,
    changed_fields: set[str] | None = None,
) -> None:
    """Atomically publish a member and its ownership while retaining other writes.

    Competing creates/initializations of the same member are refused under the
    cross-process config lock. A losing writer can leave an unreferenced empty
    store, but can neither replace the winner nor adopt another store.

    Updates may name only the fields the caller actually changed, preserving
    concurrent edits to other fields. None retains full-record publication;
    creation always publishes the full record. A new binding must be included.
    """
    from dataclasses import asdict

    from kiro_crew.config.loader import _invalidate_config_cache, update_config_locked

    unchanged_binding = not create and config.agents[member].memory_store == expected_store
    store = (
        config.agents[member].memory_store
        if unchanged_binding
        else require_member_memory_store(config, member)
    )
    agent_record = asdict(config.agents[member])
    if changed_fields is not None:
        if changed_fields - agent_record.keys():
            raise UnknownMemoryStore("member update contains unknown fields")
        if not create and not unchanged_binding and "memory_store" not in changed_fields:
            raise UnknownMemoryStore("member update omitted its changed memory binding")
        if not create:
            agent_record = {
                key: value for key, value in agent_record.items() if key in changed_fields
            }
    store_record = (
        asdict(config.memory_stores[store])
        if not unchanged_binding and store != DEFAULT_MEMORY_STORE
        else None
    )

    def mutate(data: dict) -> dict:
        agents = data.setdefault("agents", {})
        stores = data.setdefault("memory_stores", {})
        if not isinstance(agents, dict) or not isinstance(stores, dict):
            raise UnknownMemoryStore("agent or memory store configuration is unreadable")
        if DEFAULT_MEMORY_STORE not in agents and DEFAULT_MEMORY_STORE in config.agents:
            agents[DEFAULT_MEMORY_STORE] = asdict(config.agents[DEFAULT_MEMORY_STORE])
        current = agents.get(member)
        if create and member in agents:
            raise MemberAlreadyExists(
                f"Crew Member {member!r} was created concurrently; reload the roster"
            )
        if not create and current is None:
            raise UnknownMemoryStore(
                f"Crew Member {member!r} was removed concurrently; reload the roster"
            )
        if not create and current is not None:
            if (
                not isinstance(current, dict)
                or current.get("memory_store", DEFAULT_MEMORY_STORE) != expected_store
            ):
                raise UnknownMemoryStore(
                    f"Crew Member {member!r} memory changed concurrently; reload the roster"
                )
        if store_record is not None:
            # This is the second half of failed-publication cleanup's lock
            # ordering.  A publisher can validate its in-memory store, pause,
            # and then lose this config lock to cleanup.  Rechecking the
            # retirement marker while holding the lock means exactly one side
            # wins: an earlier publication is visible to cleanup, while an
            # earlier cleanup prevents a stale publisher reviving the store.
            require_member_memory_not_archived(store, expected_owner=member)
            # Retired generations may retain an unbound store with the same
            # owner display name.  Store identity is the random store key, and
            # only the newly created key is bound here; never infer a binding
            # from an older owner_member value.
            for name, entry in agents.items():
                if (
                    name != member
                    and isinstance(entry, dict)
                    and entry.get("memory_store") == store
                ):
                    raise UnknownMemoryStore(
                        f"memory store {store!r} is already bound to another member"
                    )
            existing = stores.get(store)
            if existing is not None and (
                not isinstance(existing, dict) or existing.get("owner_member") != member
            ):
                raise UnknownMemoryStore(f"memory store {store!r} ownership changed concurrently")
            stores[store] = {**(existing or {}), **store_record}
        agents[member] = {**(current or {}), **agent_record}
        return data

    update_config_locked(mutate=mutate)
    _invalidate_config_cache()
