"""Give a kept member memory store a live owner again by creating a Bot on it.

Deleting a Bot keeps its V2 store, but every route into a V2 store resolves it
through its live owner. A NEW Bot created on the store's recorded ``member_id``
is the one way back: nothing is copied and no identity is rewritten. The rules
for what may be adopted are in ``docs/system-specs/modules/memory-skills-hooks.md``.
"""

from __future__ import annotations

from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MEMORY_DB_FILE,
    UnknownMemoryStore,
    _named_store_dir,
    memory_store_name_defect,
    memory_store_namespace_lock,
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


def ownerless_defect(config, store: str) -> str | None:
    """Why *store* is not an ownerless kept member store of *config*, or ``None``."""
    return _ownerless_defect(config.agents, config.memory_stores, store)


def ownerless_member_stores(config) -> list[dict[str, str]]:
    """Every kept member store a new crewmate could start from, sorted by name.

    Each row is ``{"name": <store>, "former_owner": <name>}``: the name the
    deleted Bot showed (:func:`stamp_former_owner`), else the record's
    ``owner_member``, shown so a person can tell the stores apart; it never
    picks one. A store whose directory is
    gone, or cannot be resolved (one redirected through a link), is left out,
    since there is nothing to give back.
    """
    rows = []
    for store in sorted(config.memory_stores):
        if not isinstance(store, str):
            continue
        if ownerless_defect(config, store) is not None:
            continue
        try:
            usable = _named_store_dir(store).is_dir()
        except (UnknownMemoryStore, OSError, RuntimeError):
            usable = False
        if not usable:
            continue
        record = config.memory_stores[store]
        former = getattr(record, "former_owner", "") or getattr(record, "owner_member", "")
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
    record.former_owner = ""
    return store


def adopt_member_memory(config, member: str, store: str) -> str:
    """Bind the new *member* to the kept *store*, checked under the namespace lock.

    The lock keeps two creates from both passing the check;
    :func:`persist_member_config` re-checks the binding against the config on
    disk under its own lock before anything is written. An adopt never
    allocates, so a failed create has nothing to retire.
    """
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

    if set(getattr(config, "degraded_sections", ())) & {"memory", DEGRADED_WHOLE_CONFIG}:
        raise UnknownMemoryStore("Memory configuration is unreadable")
    with memory_store_namespace_lock():
        return _adopt_member_store(config, member, store)


def turn_off_kept_check_ins(member_id: str, member: str) -> None:
    """Turn off the check-ins the adopted identity's goal record still has on.

    The delete that kept the store removed those check-in jobs and kept the
    record, so a switch left on names a check-in with no job behind it, and the
    next save of any switch would bring it back unasked.
    """
    from kiro_crew import member_goals

    schedules = member_goals.read_goals(member_id, member)["schedules"]
    on = {cadence: False for cadence in member_goals.CADENCES if schedules.get(cadence)}
    if on:
        member_goals.set_schedules(member_id, member, on)


def stamp_former_owner(doc: dict, entry: object, name: str) -> None:
    """Record on the V2 store of the Bot *name* being deleted the name it showed.

    Called inside the delete's locked write, before the record goes. The store's
    ``owner_member`` is the config key, which for a Bot named in a non-Latin
    script is a hash; this is the name :func:`ownerless_member_stores` offers
    the kept memory under.
    """
    if not isinstance(entry, dict):
        return
    stores = doc.get("memory_stores")
    store = entry.get("memory_store")
    if not isinstance(stores, dict) or not isinstance(store, str):
        return
    record = stores.get(store)
    if isinstance(record, dict) and record.get("memory_version") == 2:
        label = entry.get("display_name")
        record["former_owner"] = label if isinstance(label, str) and label else name
