"""The roster half of an instance import's per-Bot restore.

Two steps. :func:`merge_roster` runs in a Merge into an install that already has a
``config.json``, right after the named-store merge: it keeps each archive Bot that is
the same Bot as a destination entry (an install merging its own export), adds each
archive Bot whose identity, private memory store and own workspace are all free here,
and names every other one with the reason it was skipped. :func:`imported_bots` then
derives, for each Bot the import installed or kept (by either mode), the DM-thread
slot the later steps need, through the dashboard's own slot rule.

The archive config is untrusted input. Nothing here copies a value from it into a
path, an added ``agents`` entry carries only fields that validate as
``KiroCrewAgentConfig`` fields of their declared type (:func:`merged_entry`), an
added ``memory_stores`` entry only ``MemoryStoreConfig`` ones
(:func:`merged_store_entry`), and the roster write is add-only: three new keys per Bot (its ``agents`` entry, its
``memory_stores`` entry and its ``workspaces`` entry), never a change to a key the
destination already has.
"""

from __future__ import annotations

import copy
import functools
import logging
import math
import os
import typing
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.portability_bot_types import (
    LOAD_CONFIG,
    ImportedBot,
    PartOutcome,
    is_reserved_bot_name,
    raw_entry_identity,
    raw_entry_slug,
)

logger = logging.getLogger(__name__)


#: Archive ``agents`` fields an added entry never carries: the destination's defaults
#: apply, as for a Bot a create path makes. ``kiro_agent`` is set, not copied.
_NOT_COPIED_FIELDS: frozenset[str] = frozenset(
    {
        "approval_mode",
        "acp_backend",
        "model",
        "worker_model",
        "reasoning_effort",
        "worker_reasoning_effort",
        "dynamic_capabilities",
    }
)


@dataclass(frozen=True)
class RosterMerge:
    """What :func:`merge_roster` did: the Bot names it added or kept, and one row per skip.

    ``kept`` names each archive Bot that is the same Bot as a destination entry
    (:func:`same_bot`); its destination entry is left as it is.
    """

    added: list[str] = field(default_factory=list)
    skipped: list[PartOutcome] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def _skip(name: str, reason: str) -> PartOutcome:
    return PartOutcome(bot=name, part="roster", status="skipped", reason=reason)


def _section(data: dict, key: str) -> dict | None:
    """The raw ``data[key]`` table, ``{}`` when absent, ``None`` when it is not an object."""
    value = data.get(key)
    if value is None:
        return {}
    return value if isinstance(value, dict) else None


def _existing_slug(name: str, entry: Any) -> str | None:
    """:func:`raw_entry_slug` of a destination agent, ``None`` when it has no valid one."""
    try:
        return raw_entry_slug(name, entry)
    except (ValueError, TypeError):
        return None


def same_bot(name: str, entry: Any, destination_agents: Mapping[str, Any]) -> bool:
    """Whether archive Bot *name* is the same Bot as the destination's entry of that name.

    True when the destination has an entry under exactly *name* and both entries have
    the same identity (:func:`raw_entry_identity`): the same ``member_id`` and the same
    ``memory_store``. A version-1 Bot has an empty ``member_id`` on both sides. That is
    what an install merging its own export finds; any other name match is a different
    Bot that only shares the name.
    """
    if name not in destination_agents:
        return False
    identity = raw_entry_identity(entry)
    return identity is not None and identity == raw_entry_identity(destination_agents[name])


@functools.lru_cache(maxsize=1)
def _agent_field_types() -> dict[str, Any]:
    from kiro_crew.config.sections_session import KiroCrewAgentConfig

    return dict(typing.get_type_hints(KiroCrewAgentConfig))


def _field_value_ok(kind: Any, value: Any) -> bool:
    if kind is bool:
        return isinstance(value, bool)
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is float:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    if kind in (str, dict):
        return isinstance(value, kind)
    return False


@functools.lru_cache(maxsize=1)
def _store_field_types() -> dict[str, Any]:
    from kiro_crew.config.memory_sections import MemoryStoreConfig

    return dict(typing.get_type_hints(MemoryStoreConfig))


def merged_store_entry(name: str, member_id: str, entry: Any) -> dict | None:
    """The ``memory_stores`` entry a Merge adds for archive Bot *name*, or ``None``.

    Validated as :func:`merged_entry` validates the agent entry: ``owner_member`` is
    the Bot's name, whatever the archive says; ``owner_member_id`` must equal
    *member_id* and ``memory_version`` must be the integer 2; every other key must be
    a ``MemoryStoreConfig`` field and its value of that field's declared type, else
    the entry is rejected.
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("owner_member_id") != member_id or not member_id:
        return None
    version = entry.get("memory_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 2:
        return None
    types = _store_field_types()
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key == "owner_member":
            continue
        if not isinstance(key, str) or key not in types or not _field_value_ok(types[key], value):
            return None
        out[key] = copy.deepcopy(value)
    out["owner_member"] = name
    return out


def merged_entry(name: str, entry: Any) -> dict | None:
    """The ``agents`` entry a Merge adds for archive Bot *name*, or ``None`` when it is rejected.

    What a create path sets for a Bot that runs as its own agent: ``kiro_agent`` is
    the Bot's own name (its own crewmate file), whatever the archive says;
    ``approval_mode``, ``acp_backend``, ``model``, ``worker_model``,
    ``reasoning_effort``, ``worker_reasoning_effort`` and ``dynamic_capabilities``
    are not copied, so this
    install's defaults apply. Every other key must be a ``KiroCrewAgentConfig`` field and its value of
    that field's declared type (``bool`` is no ``int``; a ``float`` is finite), else
    the whole entry is rejected.
    """
    if not isinstance(entry, dict):
        return None
    types = _agent_field_types()
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key in _NOT_COPIED_FIELDS or key == "kiro_agent":
            continue
        if not isinstance(key, str) or key not in types or not _field_value_ok(types[key], value):
            return None
        out[key] = copy.deepcopy(value)
    out["kiro_agent"] = name
    return out


def _candidate_reason(
    name: str,
    entry: Any,
    archive_stores: Mapping[str, Any],
    stores_added: Collection[str],
    stores_root: Path,
    doc: dict,
) -> str:
    """Why the archive Bot *name* cannot be added to *doc*, or ``""`` when it can.

    *doc* is the destination config as read under the config lock, including every
    Bot this same mutation already added, so two archive Bots that collide with each
    other are decided as if the first had been there all along.
    """
    from kiro_crew.member_workspace import DEFAULT_WORKSPACE
    from kiro_crew.members import (
        MemberSlugError,
        is_dispatchable_member_name,
        is_valid_member_name,
        validate_slug,
    )
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, memory_store_name_defect
    from kiro_crew.validation import WORKSPACE_NAME_RE

    if not (
        isinstance(entry, dict) and is_valid_member_name(name) and is_dispatchable_member_name(name)
    ):
        return "config_rejected"
    member_id = entry.get("member_id")
    if member_id in (None, ""):
        return "no_member_id"
    if not isinstance(member_id, str):
        return "config_rejected"
    try:
        slug = validate_slug(member_id)
    except MemberSlugError:
        return "config_rejected"
    store = entry.get("memory_store")
    if (
        not isinstance(store, str)
        or store == DEFAULT_MEMORY_STORE
        or memory_store_name_defect(store) is not None
    ):
        return "config_rejected"
    if merged_store_entry(name, member_id, archive_stores.get(store)) is None:
        return "config_rejected"
    workspace = entry.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return "config_rejected"
    if workspace != DEFAULT_WORKSPACE and not WORKSPACE_NAME_RE.fullmatch(workspace):
        return "config_rejected"
    if merged_entry(name, entry) is None:
        return "config_rejected"

    agents = doc["agents"]
    folded = name.casefold()
    if any(isinstance(n, str) and (n == name or n.casefold() == folded) for n in agents):
        return "name_taken"
    if any(_existing_slug(n, e) == slug for n, e in agents.items()):
        return "slug_taken"
    if any(isinstance(e, dict) and e.get("member_id") == member_id for e in agents.values()) or any(
        isinstance(s, dict) and s.get("owner_member_id") == member_id
        for s in doc["memory_stores"].values()
    ):
        return "member_id_taken"
    if store not in stores_added:
        # A store the named-store merge did not create here: either one of this
        # install's (kept), or the archive never carried it.
        return "store_taken" if os.path.lexists(stores_root / store) else "store_not_carried"
    if store in doc["memory_stores"] or any(
        isinstance(e, dict) and e.get("memory_store") == store for e in agents.values()
    ):
        return "store_taken"
    if workspace == DEFAULT_WORKSPACE or workspace in doc["workspaces"]:
        return "workspace_taken"
    return ""


def merge_roster(
    archive_config: Mapping[str, Any],
    mc: Path,
    *,
    stores_added: Collection[str],
    teams_installed: bool = False,
) -> RosterMerge:
    """Keep each same archive Bot and add each free one, in ONE locked config write.

    *archive_config* is the vetted archive ``config.json`` object; *mc* the data
    home; *stores_added* the names of the stores THIS import's named-store merge
    created under ``mc / memory_stores``. The reserved ``default`` agent is never a
    Bot and is not considered. An archive Bot that is the same Bot as the destination
    entry of its name (:func:`same_bot`) is ``kept``: nothing is written for it, and
    the import goes on as for a Bot it brought. Any other Bot is added only when its name (exact
    and case-folded), its slug, its non-empty ``member_id``, its non-default memory
    store (config entry, and the directory is one *stores_added* names) and its
    non-default workspace name are all free here, and no file the import did not
    write would run it: a Bot whose name has a crewmate file the owner record gives
    another data home is skipped first, with ``foreign_owner``, and one whose name
    has a user-level agent file or another crewmate file that is not this data
    home's is skipped (``portability_bot_crewmates.crewmate_name_held``). Those
    scans run once per name, before the config lock is taken (the mutation can run
    again); every config condition is checked against the document as read under
    the config lock, so a concurrent create wins and the archive Bot is skipped. A Bot whose workspace directory under the
    data home already exists is skipped with ``workspace_taken``: a folder already
    here is never adopted. For each Bot that passed every check, the mutation adds a
    ``workspaces`` entry naming a fresh directory under the data home (the archive's
    own ``dir`` is never used: it is a path the archive chose) and then, right
    before the Bot is written, runs ``crew_teams.release_for_create`` as every create
    path does, so the new Bot starts on no team; ``TeamsUnavailable`` withdraws the
    workspace entry and skips that Bot with ``write_failed``. With *teams_installed*
    (this import installed the archive's ``crew-teams`` document) that step is not
    run: the memberships it lists are the imported Bots' own. The mutation then adds
    its ``agents`` entry (cut back by :func:`merged_entry`) and its ``memory_stores``
    entry (owned by its ``member_id``, cut back by :func:`merged_store_entry`). Once
    the write is decided, :func:`_settle_added` removes again an empty workspace
    directory the mutation made for a Bot that was not added.

    A destination entry is never replaced: an archive Bot whose name is taken by
    another Bot is skipped with ``name_taken`` whatever that Bot looks like, and
    nothing of the destination Bot changes (no entry, no store entry, no team
    membership).

    Never raises: a refused write skips every Bot it would have added.
    """
    from kiro_crew import crew_teams
    from kiro_crew.config.loader import _invalidate_config_cache, update_config_locked
    from kiro_crew.config.paths import config_dir
    from kiro_crew.config.sections_session import WorkspaceConfig
    from kiro_crew.member_workspace import (
        MemberWorkspaceUnavailable,
        publish_member_workspace,
        workspace_dir_name,
    )
    from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
    from kiro_crew.portability_bot_crewmates import crewmate_name_held

    raw_agents = archive_config.get("agents")
    raw_stores = archive_config.get("memory_stores")
    archive_agents: dict = raw_agents if isinstance(raw_agents, dict) else {}
    archive_stores: dict = raw_stores if isinstance(raw_stores, dict) else {}
    if not archive_agents:
        return RosterMerge()
    added_set = frozenset(s for s in stores_added if isinstance(s, str))
    target = mc / "config.json"
    stores_root = mc / MEMORY_STORES_DIR_NAME
    names = [n for n in archive_agents if isinstance(n, str) and not is_reserved_bot_name(n)]
    # The user-agent and crewmate-file scans, once: the mutation below can run again.
    held = {name: _held_reason(crewmate_name_held, name) for name in names}
    added: list[str] = []
    kept: list[str] = []
    skipped: dict[str, PartOutcome] = {}
    # Workspace directories the mutation made that were not there before it, by Bot.
    made: dict[str, Path] = {}
    # What the last run of the mutation wrote per added Bot, to tell after a raised
    # write whether it landed: (agents entry, store name, store entry, workspace).
    wrote: dict[str, tuple[dict, str, dict, str]] = {}

    def mutate(data: dict) -> dict | None:
        added.clear()
        kept.clear()
        skipped.clear()
        wrote.clear()
        sections = {key: _section(data, key) for key in ("agents", "memory_stores", "workspaces")}
        if not os.path.lexists(target) or any(v is None for v in sections.values()):
            for name in names:
                skipped[name] = _skip(name, "config_rejected")
            return None
        doc: dict[str, dict] = {k: v for k, v in sections.items() if v is not None}
        for name in names:
            entry = archive_agents[name]
            if same_bot(name, entry, doc["agents"]):
                kept.append(name)
                continue
            try:
                reason = (
                    "foreign_owner"
                    if held[name] == "foreign_owner"
                    else _candidate_reason(name, entry, archive_stores, added_set, stores_root, doc)
                    or held[name]
                )
            except Exception:  # noqa: BLE001 - archive-authored; refuses this one Bot
                logger.warning("import: archive Bot %r could not be judged", name, exc_info=True)
                reason = "config_rejected"
            if reason:
                skipped[name] = _skip(name, reason)
                continue
            store = entry["memory_store"]
            workspace = entry["workspace"]
            directory = config_dir() / workspace_dir_name(workspace)
            if os.path.lexists(directory) and made.get(name) != directory:
                # A folder already here is never adopted as an added Bot's workspace:
                # its files would become that Bot's.
                skipped[name] = _skip(name, "workspace_taken")
                continue
            try:
                # Re-checks the name and the directory against this document and
                # makes the directory; a collision refuses this one Bot.
                publish_member_workspace(
                    data, workspace, WorkspaceConfig(dir=workspace_dir_name(workspace))
                )
            except MemberWorkspaceUnavailable:
                skipped[name] = _skip(name, "workspace_taken")
                continue
            made[name] = directory
            if not teams_installed:
                try:
                    # Inside the config lock, after every check for this Bot passed
                    # and right before its name is registered (the contract of
                    # crew_teams.release_name).
                    crew_teams.release_for_create(name)
                except crew_teams.TeamsUnavailable:
                    data["workspaces"].pop(workspace, None)
                    skipped[name] = _skip(name, "write_failed")
                    continue
            doc["workspaces"] = data["workspaces"]
            store_entry = merged_store_entry(name, entry["member_id"], archive_stores[store])
            agent_entry = merged_entry(name, entry)
            doc["memory_stores"][store] = store_entry
            doc["agents"][name] = agent_entry
            if agent_entry is not None and store_entry is not None:
                wrote[name] = (
                    copy.deepcopy(agent_entry),
                    store,
                    copy.deepcopy(store_entry),
                    workspace,
                )
            added.append(name)
        if not added:
            return None
        for key, value in doc.items():
            data[key] = value
        return data

    try:
        update_config_locked(target, mutate=mutate)
    except Exception as exc:  # the import has already installed stores; report, never abort
        logger.warning("import: the roster merge raised: %s", exc, exc_info=True)
        reason = "write_failed" if isinstance(exc, OSError) else "config_rejected"
        # The write can raise after the document landed (a later step of the
        # locked update failed), so the config is read again: a Bot whose entries
        # are there as this mutation wrote them is added.
        landed = _landed(mc, wrote)
        # A kept Bot needed no write: the destination entry the decision read is
        # still there.
        for name in names:
            if name in landed:
                continue
            if name in added or (name not in skipped and name not in kept):
                skipped[name] = _skip(name, reason)
        added[:] = [name for name in added if name in landed]
    if added:
        _invalidate_config_cache()
    _settle_added(added, made)
    ordered = [skipped[n] for n in names if n in skipped]
    return RosterMerge(added=list(added), skipped=ordered, kept=list(kept))


def _held_reason(scan: Any, name: str) -> str:
    """``scan(name)`` (the crewmate-name pre-scan); ``config_rejected`` when it raises."""
    try:
        return str(scan(name))
    except Exception:  # noqa: BLE001 - refuses this one Bot, never the others
        logger.warning("import: the crewmate file of %r could not be checked", name, exc_info=True)
        return "config_rejected"


def _landed(mc: Path, wrote: Mapping[str, tuple[dict, str, dict, str]]) -> frozenset[str]:
    """The Bots of *wrote* whose entries ``mc / config.json`` holds exactly as written.

    Read as a document after a roster write raised. A Bot counts as landed only when
    its ``agents`` entry and its ``memory_stores`` entry are equal to what the
    mutation wrote and its ``workspaces`` entry is there. Empty when the config
    cannot be read: then no Bot is reported added.
    """
    from kiro_crew.portability_bot_export import read_config_document

    if not wrote:
        return frozenset()
    try:
        doc = read_config_document(str(mc))
    except Exception:  # noqa: BLE001 - unread: nothing is known to have landed
        logger.warning("import: the config could not be read again", exc_info=True)
        return frozenset()
    if not isinstance(doc, Mapping):
        return frozenset()
    agents, stores, workspaces = (doc.get(k) for k in ("agents", "memory_stores", "workspaces"))
    if not (
        isinstance(agents, Mapping)
        and isinstance(stores, Mapping)
        and isinstance(workspaces, Mapping)
    ):
        return frozenset()
    return frozenset(
        name
        for name, (agent, store, store_entry, workspace) in wrote.items()
        if agents.get(name) == agent
        and stores.get(store) == store_entry
        and workspace in workspaces
    )


def _settle_added(added: Collection[str], made: Mapping[str, Path]) -> None:
    """After the roster write: drop the workspace directories it did not use.

    A directory in *made* belongs to a Bot the write did not add (the write failed
    or the mutation ran again without it) unless that Bot is in *added*; it is
    removed when it is still empty, and left where it is otherwise.
    """
    for name, directory in made.items():
        if name in added:
            continue
        try:
            os.rmdir(directory)
        except OSError:
            logger.debug("import: workspace directory %s left in place", directory, exc_info=True)


def imported_bots(
    mc: Path, names: Iterable[str], *, config: Any = LOAD_CONFIG
) -> tuple[list[ImportedBot], list[PartOutcome]]:
    """Each Bot of *names* as the destination config holds it after the import, with its DM slot.

    *mc* is the data home the import wrote. *config* is its loaded config, which the
    import reads once after its mode branch; ``None`` says it could not be read
    (every Bot is ``unreadable_record``), and the default reads ``config.json``
    through the ordinary loader (the cache is dropped first, so the import's own
    write is seen).
    The slot key and its memory generation come from the dashboard's own rule,
    ``members_thread._member_thread_slot``, so the import derives exactly the key an
    open of the thread would. A Bot whose store does not resolve as its own
    (``UnknownMemoryStore`` or ``ValueError`` from that rule) is reported with reason
    ``store_not_owned``; any other failure for one Bot with ``unreadable_record``.
    The second list holds only those rows. Never raises.
    """
    bots: list[ImportedBot] = []
    outcomes: list[PartOutcome] = []
    wanted = [n for n in names if isinstance(n, str)]
    try:
        from kiro_crew import members as members_mod
        from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
        from kiro_crew.config.paths import config_dir
        from kiro_crew.dashboard.handlers.members_thread import _member_thread_slot

        if Path(os.path.realpath(mc)) != Path(os.path.realpath(config_dir())):
            raise ValueError(f"{mc} is not this process's data home")
        if config is None:
            raise ValueError("the installed config could not be read")
        if config is LOAD_CONFIG:
            _invalidate_config_cache()
            config = KiroCrewConfig.load()
        cfg = config
    except Exception:
        logger.warning("import: the installed roster could not be read", exc_info=True)
        return [], [PartOutcome(n, "roster", "skipped", "unreadable_record") for n in wanted]

    for name in wanted:
        try:
            agent = cfg.agents.get(name)
            if agent is None:
                raise LookupError(f"Bot {name!r} is not in the installed roster")
            member_id = agent.member_id
            store = agent.memory_store
            if not isinstance(member_id, str) or not isinstance(store, str):
                raise TypeError("member identity or memory binding is not a string")
            slug = members_mod.member_slug(name, cfg)
        except Exception:
            logger.warning("import: Bot %r could not be read", name, exc_info=True)
            outcomes.append(PartOutcome(name, "roster", "skipped", "unreadable_record"))
            continue
        try:
            slot_key, generation = _member_thread_slot(cfg, name, slug)
        except ValueError:  # UnknownMemoryStore included
            outcomes.append(PartOutcome(name, "roster", "skipped", "store_not_owned"))
            continue
        except Exception:
            logger.warning("import: Bot %r has no derivable DM slot", name, exc_info=True)
            outcomes.append(PartOutcome(name, "roster", "skipped", "unreadable_record"))
            continue
        bots.append(
            ImportedBot(
                name=name,
                slug=slug,
                member_id=member_id,
                memory_store=store,
                generation=generation,
                slot_key=slot_key,
                kiro_agent=agent.kiro_agent,
            )
        )
    return bots, outcomes
