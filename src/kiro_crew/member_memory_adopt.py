"""Give a kept member memory store a live owner again by creating a crewmate on it.

Deleting a crewmate removes its record and keeps its V2 store: the store's config
record, its directory (``memory.db`` stamped with ``(member_id, store)``) and its
backups. Every route into a V2 store resolves it through its live owner, so a kept
store with no owner cannot be opened, exported or restored.

This module is the one supported way back: a NEW crewmate is created bound to the
kept store's existing ``member_id``. Nothing is copied and no identity is
rewritten -- the database keeps the identity it was stamped with, the store keeps
its name, and only the store record's display ``owner_member`` follows the new
name. :func:`kiro_crew.memory_stores.persist_member_config` already accepts that
binding on a create (it resolves the store through the new record and
compare-and-sets ``owner_member_id``), so this module only builds it.

It is offered only for an OWNERLESS store, and only when asked for by name:

- a V2 record whose ``owner_member_id`` is a non-empty string;
- no configured crewmate holds that ``member_id`` or binds the store;
- no legacy crewmate (empty ``member_id``) derives that id as its slug, since the
  two would then share one member slug;
- the store's database carries exactly ``(owner_member_id, store)``;
- no retained chat-thread binding under that slug names a crewmate by name
  only (such a binding would not follow the new name, so the thread would be
  refused on open).

A name match never adopts: a same-named create still gets a fresh store
(``_allocate_member_id`` keeps reserving kept ids). Export, restore and import
keep requiring a live owner, which the adopted crewmate then is.
"""

from __future__ import annotations

from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MEMORY_DB_FILE,
    UnknownMemoryStore,
    _named_store_dir,
    memory_store_name_defect,
    memory_store_namespace_lock,
    provision_member_memory,
)


def _owner_id_of(record: object) -> str:
    """The record's ``owner_member_id`` when it is a usable identity, else ``""``."""
    version = getattr(record, "memory_version", 1)
    if type(version) is not int or version != 2:
        return ""
    owner_id = getattr(record, "owner_member_id", "")
    return owner_id if isinstance(owner_id, str) else ""


def _ownerless_defect(agents, stores, store: str) -> str | None:
    """Why *store* is not an ownerless kept member store, or ``None`` when it is.

    Config-only: the database identity is checked separately, because reading
    it opens a file and the listing should stay cheap.
    """
    from kiro_crew.members import MemberSlugError, slug_for_name

    if store == DEFAULT_MEMORY_STORE or memory_store_name_defect(store) is not None:
        return "is not a member memory store"
    record = stores.get(store)
    if record is None:
        return "is not declared"
    owner_id = _owner_id_of(record)
    if not owner_id:
        return "is not a member memory store"
    for name, agent in agents.items():
        member_id = getattr(agent, "member_id", "")
        if member_id == owner_id or getattr(agent, "memory_store", "") == store:
            return "already belongs to a crewmate"
        if member_id == "":
            try:
                if slug_for_name(name) == owner_id:
                    return "shares its identity with an existing crewmate"
            except MemberSlugError:
                continue
    return None


def ownerless_member_stores(config) -> list[dict[str, str]]:
    """Every kept member store a new crewmate could start from, sorted by name.

    Each row is ``{"name": <store>, "former_owner": <owner_member>}``. The
    former owner is the display name the record last carried, shown so a person
    can tell the stores apart; it is never used to pick one. A store whose
    directory is gone is left out, since there is nothing to give back.
    """
    rows = []
    for store in sorted(config.memory_stores):
        if not isinstance(store, str):
            continue
        if _ownerless_defect(config.agents, config.memory_stores, store) is not None:
            continue
        if not _named_store_dir(store).is_dir():
            continue
        former = getattr(config.memory_stores[store], "owner_member", "")
        rows.append({"name": store, "former_owner": former if isinstance(former, str) else ""})
    return rows


def _adopt_member_store(config, member: str, store: str) -> str:
    """Bind the new *member* to the kept *store*'s identity, in *config* only."""
    from kiro_crew.members import read_dm_binding
    from kiro_crew.vector_memory import read_member_database_identity, sqlite3

    if member == DEFAULT_MEMORY_STORE or member not in config.agents:
        raise UnknownMemoryStore("An existing non-default member is required")
    agent = config.agents[member]
    if agent.member_id or agent.memory_store not in ("", DEFAULT_MEMORY_STORE):
        raise UnknownMemoryStore("Only a new crewmate can start from kept memory")
    others = {name: item for name, item in config.agents.items() if name != member}
    defect = _ownerless_defect(others, config.memory_stores, store)
    if defect is not None:
        raise UnknownMemoryStore(f"Memory store {store!r} {defect}; nothing was adopted")
    target = _named_store_dir(store)
    if not target.is_dir():
        raise UnknownMemoryStore(f"Memory store {store!r} is missing; nothing was adopted")
    record = config.memory_stores[store]
    owner_id = record.owner_member_id
    try:
        identity = read_member_database_identity(target / MEMORY_DB_FILE)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise UnknownMemoryStore(
            f"Memory store {store!r} is unreadable; nothing was adopted"
        ) from exc
    if identity != (owner_id, store):
        raise UnknownMemoryStore(
            f"Memory store {store!r} does not match its declaration; nothing was adopted"
        )
    # Before the new record exists, a binding that carries this id resolves to
    # no one and reads as absent. One that still reads is keyed by a former
    # display name instead, which would not follow the new crewmate.
    if read_dm_binding(owner_id) is not None:
        raise UnknownMemoryStore(
            f"Memory store {store!r} has a chat thread kept under another name; "
            "nothing was adopted"
        )
    agent.member_id = owner_id
    agent.memory_store = store
    record.owner_member = member
    return store


def provision_or_adopt_member_memory(config, member: str, adopt_store: str = "") -> str:
    """Give the new *member* its memory: a fresh store, or the kept *adopt_store*.

    With no *adopt_store* this is :func:`provision_member_memory`. With one, the
    store is checked and bound under the namespace lock, so two creates cannot
    both pass the check; :func:`persist_member_config` re-checks the binding
    against the config on disk under its own lock before anything is written.
    An adopt never allocates, so a failed create has nothing to retire.
    """
    if not adopt_store:
        return provision_member_memory(config, member)
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

    if set(getattr(config, "degraded_sections", ())) & {"memory", DEGRADED_WHOLE_CONFIG}:
        raise UnknownMemoryStore("Memory configuration is unreadable")
    with memory_store_namespace_lock():
        return _adopt_member_store(config, member, adopt_store)
