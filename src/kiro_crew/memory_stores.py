"""Named V1 stores and explicitly created member V2 stores.

V1 paths retain their existing layout: Global markdown in workspace/memory,
Global FTS in memory_index.db, and vectors in memory.db. Named V1 stores keep
those files under memory_stores/<store>.

A V2 member owns one memory.db with its immutable member/store identity and
all learned memory tables. Its manual preferences/projects documents remain
separate. FTS uses that same database. Only explicit new-member creation
allocates files; resolution never repairs, migrates, copies or initializes data.
Config membership and captured execution records route built-in operations;
these paths are not a confidentiality boundary against same-user code.

This module keeps only stdlib imports at module scope to avoid early security
and configuration import cycles.
"""

from __future__ import annotations

import logging
import os
import re
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

#: Historical credential/diagnostic filenames remain excluded from snapshot
#: imports and exports. They are never routing authorities and new executions
#: do not create them. Regular rolling backups and pending restore journals
#: are also host-local, as with the default store's <home>/backups directory.
MEMBER_API_KEY_FILE = ".member-api-key"
MEMBER_BACKUPS_DIR_NAME = ".member-backups"
EXECUTION_LOGS_DIR_NAME = ".execution-logs"
#: A NAMED V1 store's rolling backups sit inside its own directory (``memory_backup``
#: aliases this); a V2 member's sit under :data:`MEMBER_BACKUPS_DIR_NAME` instead.
STORE_BACKUP_DIR_NAME = "backups"
_HOST_LOCAL_ROOT_ENTRIES: frozenset[str] = frozenset(
    {
        MEMBER_API_KEY_FILE,
        MEMBER_BACKUPS_DIR_NAME,
        EXECUTION_LOGS_DIR_NAME,
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

    A named V1 store's index lives beside its markdown tree. V2 returns its
    existing ``memory.db`` because its FTS index and learned memory share that
    database. Snapshot and export include the named store's directory.

    The V1 index is fully DERIVED — ``MemoryStore.rebuild_index`` regenerates it
    from preferences.md, projects.md and history/*.md and reads no index state —
    so a store whose index is not backed up loses search results until the next
    rebuild, never memory.
    """
    from kiro_crew.memory import INDEX_DB_FILE

    name = resolve_declared_store(store)
    if memory_store_version(name) == 2:
        return resolve_store_path(name)
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
    """Routine maintenance targets include every existing member store."""
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
        if getattr(record, "memory_version", 1) == 2:
            member_id = getattr(record, "owner_member_id", "")
            owners = [
                alias
                for alias, member in config.agents.items()
                if getattr(member, "member_id", "") == member_id
            ]
            if not member_id or len(owners) != 1 or bindings.get(name) != owners:
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


def member_memory_identity(store: str) -> tuple[str, int]:
    """Read the canonical database identity without any filesystem manifest."""
    from kiro_crew.vector_memory import read_member_database_identity

    member_id, store_id = read_member_database_identity(_named_store_dir(store) / MEMORY_DB_FILE)
    if store_id != store:
        raise UnknownMemoryStore("Member database store identity does not match its location")
    return member_id, 2


def memory_store_version(store: str) -> int:
    """Use the explicit declaration; an invalid member store never becomes V1."""
    if store in ("", DEFAULT_MEMORY_STORE):
        return 1
    from kiro_crew.config.loader import KiroCrewConfig

    record = KiroCrewConfig.load().memory_stores.get(validate_memory_store_name(store))
    if (
        record is None
        or type(record.memory_version) is not int
        or record.memory_version not in (1, 2)
    ):
        raise UnknownMemoryStore("Memory store declaration is unavailable")
    return record.memory_version


def _require_legacy_store_files(store: str, target: Path) -> None:
    """An explicit member database cannot be opened through the V1 API."""
    import sqlite3

    database = target / MEMORY_DB_FILE
    if not database.exists():
        return
    if not database.is_file():
        raise UnknownMemoryStore(f"memory store {store!r} is unreadable")
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            if connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='member_database'"
            ).fetchone():
                raise UnknownMemoryStore(
                    f"memory store {store!r} is a member database; V1 was not used"
                )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise UnknownMemoryStore(f"memory store {store!r} is unreadable") from exc


def require_memory_store(store: str, *, config=None, require_directory: bool = True) -> str:
    """Validate a declared store and its exact persisted database identity."""
    validate_memory_store_name(store)
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    if store == DEFAULT_MEMORY_STORE:
        return store
    record = config.memory_stores.get(store)
    if (
        record is None
        or type(record.memory_version) is not int
        or record.memory_version not in (1, 2)
    ):
        raise UnknownMemoryStore(f"Memory store {store!r} is unavailable; Global was not used")
    target = _named_store_dir(store)
    if require_directory and not target.is_dir():
        raise UnknownMemoryStore(f"Memory store {store!r} is missing; Global was not used")
    if record.memory_version == 2:
        from kiro_crew.execution_context import member_config_for_id

        member_id = getattr(record, "owner_member_id", "")
        _alias, member = member_config_for_id(config, member_id)
        if member.memory_store != store:
            raise UnknownMemoryStore("Member and store declarations disagree")
        if sum(agent.memory_store == store for agent in config.agents.values()) != 1:
            raise UnknownMemoryStore("A member store cannot be shared")
        if require_directory:
            from kiro_crew.vector_memory import read_member_database_identity, sqlite3

            try:
                identity = read_member_database_identity(target / MEMORY_DB_FILE)
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise UnknownMemoryStore(
                    "Member database is missing or unreadable; Global was not used"
                ) from exc
            if identity != (member_id, store):
                raise UnknownMemoryStore("Member database identity does not match its declaration")
    elif getattr(record, "owner_member_id", "") or record.owner_member:
        raise UnknownMemoryStore("A V1 store cannot carry a member identity")
    elif require_directory:
        _require_legacy_store_files(store, target)
    return store


def require_member_memory_store(config, member: str, *, require_directory: bool = True) -> str:
    """Resolve the explicitly selected member, without guessing from a template."""
    if member == DEFAULT_MEMORY_STORE:
        return DEFAULT_MEMORY_STORE
    agent = config.agents.get(member)
    if agent is None:
        raise UnknownMemoryStore(f"Unknown Crew Member {member!r}; Global was not used")
    store = require_memory_store(
        agent.memory_store, config=config, require_directory=require_directory
    )
    record = config.memory_stores.get(store)
    if agent.member_id and (record is None or record.memory_version != 2):
        raise UnknownMemoryStore("The member identity has no V2 store; Global was not used")
    if record is not None and record.memory_version == 2:
        if not agent.member_id or agent.member_id != record.owner_member_id:
            raise UnknownMemoryStore("The member's immutable memory identity is unavailable")
    return store


def unusable_legacy_binding(config, member: str) -> str | None:
    """Why *member*'s V1 binding names a store no resolver composes, or ``None``.

    The one binding shape the immutability rules protect nothing on. A name that
    fails :func:`memory_store_name_defect` is dropped by :func:`usable_store_names`
    and refused by every resolver before a path is composed, so no directory under
    ``memory_stores/`` is read, replaced or removed by moving the member off it, and
    there is no legacy tree for :func:`_require_legacy_store_files` to inspect. The
    member is simply dead: every turn fails at :func:`require_member_memory_store`,
    and so would any repair that validates the binding it is about to replace.

    Answers the defect only for a record with no ownership claim
    (``owner_member == ""`` and ``memory_version == 1``) or no record at all. A
    record claiming private ownership under an unusable name is not a legacy
    binding and stays refused by the callers: ownership cannot be verified for a
    store that has no path, and a refusal is the only answer that adopts nothing.

    Pure -- no filesystem call and no config load -- so the dashboard handler and
    the CLI can both ask with the config they already hold.
    """
    agent = config.agents.get(member)
    if agent is None:
        return None
    store = agent.memory_store
    if store == DEFAULT_MEMORY_STORE:
        return None
    defect = memory_store_name_defect(store)
    if defect is None:
        return None
    record = config.memory_stores.get(store) if isinstance(store, str) else None
    if record is not None and (
        getattr(record, "owner_member", "") != ""
        or type(getattr(record, "memory_version", None)) is not int
        or record.memory_version != 1
    ):
        return None
    return defect


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


def provision_member_memory(config, member: str) -> str:
    """Validate configuration before allocating or acquiring a store write lock."""
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

    if set(getattr(config, "degraded_sections", ())) & {"memory", DEGRADED_WHOLE_CONFIG}:
        raise UnknownMemoryStore("Memory configuration is unreadable")
    with memory_store_namespace_lock():
        return _provision_member_memory(config, member)


def _provision_member_memory(config, member: str) -> str:
    from kiro_crew import platform_compat
    from kiro_crew.config.sections import MemoryStoreConfig
    from kiro_crew.members import slug_for_name
    from kiro_crew.vector_memory import create_member_database

    if member == DEFAULT_MEMORY_STORE or member not in config.agents:
        raise UnknownMemoryStore("An existing non-default member is required")
    agent = config.agents[member]
    current = config.memory_stores.get(agent.memory_store)
    if current is not None and current.memory_version == 2:
        return require_member_memory_store(config, member)
    member_id = agent.member_id
    if member_id:
        raise UnknownMemoryStore("Existing member identity has no valid store; allocation refused")
    if current is not None and (current.owner_member_id or current.owner_member):
        raise UnknownMemoryStore("Existing member store identity is invalid")
    if not member_id:
        base = slug_for_name(member)
        identities = [getattr(item, "member_id", "") for item in config.agents.values()]
        # Deleted members retain their stores; captured work must never resolve
        # their identities to a newly created member with the same slug.
        identities.extend(item.owner_member_id for item in config.memory_stores.values())
        if any(not isinstance(identity, str) for identity in identities):
            raise UnknownMemoryStore(
                "Configured member identity must be a string; allocation refused"
            )
        existing = set(identities)
        member_id = base
        while member_id in existing:
            member_id = f"{base[:48]}-{uuid.uuid4().hex[:12]}"
    root = memory_stores_root()
    platform_compat.make_owner_only_dir(root)
    while True:
        name = f"member-{member_id[:32]}-{uuid.uuid4().hex}"
        target = _named_store_dir(name)
        try:
            target.mkdir(mode=0o700, exist_ok=False)
            break
        except FileExistsError:
            continue
    create_member_database(target / MEMORY_DB_FILE, member_id=member_id, store_id=name)
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.memory import PREFERENCES_FILE, PROJECTS_FILE

    manual = target / "memory"
    manual.mkdir()
    atomic_write(manual / PREFERENCES_FILE, "# Member Preferences\n", fsync=True)
    atomic_write(manual / PROJECTS_FILE, "# Member Projects\n", fsync=True)
    agent.member_id = member_id
    agent.memory_store = name
    config.memory_stores[name] = MemoryStoreConfig(
        owner_member=member, owner_member_id=member_id, memory_version=2
    )
    return name


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
        changed_fields = set(changed_fields)
        if not unchanged_binding:
            changed_fields.add("member_id")
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
        if (
            isinstance(current, dict)
            and current.get("member_id")
            and current["member_id"] != config.agents[member].member_id
        ):
            raise UnknownMemoryStore("Member identity is immutable")
        if store_record is not None:
            # Publish one immutable member/store pair under the ordinary config lock.
            for name, entry in agents.items():
                if (
                    name != member
                    and isinstance(entry, dict)
                    and (
                        entry.get("memory_store") == store
                        or (
                            config.agents[member].member_id
                            and entry.get("member_id") == config.agents[member].member_id
                        )
                    )
                ):
                    raise UnknownMemoryStore(
                        f"memory store {store!r} is already bound to another member"
                    )
            existing = stores.get(store)
            if existing is not None and (
                not isinstance(existing, dict)
                or existing.get("owner_member_id") != config.agents[member].member_id
            ):
                raise UnknownMemoryStore(f"memory store {store!r} ownership changed concurrently")
            stores[store] = {**(existing or {}), **store_record}
        agents[member] = {**(current or {}), **agent_record}
        return data

    update_config_locked(mutate=mutate)
    _invalidate_config_cache()
