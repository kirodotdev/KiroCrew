"""Crew Mode: which agents are conductors, which sessions are Crew Mode roots, and
which sessions a root dispatched.

Every conductor is Crew Mode. :func:`conductor_agents` is the one set: every shipped conductor template (the Crew Mode conductor, the team
lead, the pipeline, security and ledger conductors), the configured
``agent.crew_mode_agent`` and the deprecated names that resolve to it. The dashboard
keeps all of them out of a normal chat's agent picker, and a ROOT session on any of
them is a Crew Mode root, so an existing conductor session (a team lead, a scheduled
pipeline conductor) is a Crew Mode root as soon as it is loaded and its workers move
to its side panel. No session, folder or transcript is deleted or moved for that.

``agent.crew_mode_agent`` is the one place that names THE conductor: the agent a chat
switches to when Crew Mode is turned on. Until the chat's first user message the
person can turn it off again (a switch back to a non-conductor template); after that
the server refuses to move a Crew Mode root onto another agent
(``crew_mode_locked``).

The chat keeps a stamp (``crew_mode_agent`` on the slot, persisted with it) only to
mark it as Crew Mode: a root on a custom conductor absent from the config
stays Crew Mode.

A **Crew Mode root** is a ROOT session (nothing dispatched it) running a conductor as
a template, never as a crewmate. Every session it dispatched, directly or through a
session it dispatched, is its **worker**: the slot list stamps each worker with
``crew_root`` (its root's key) so the person's own session list and History can
leave it out and the side panel can list it under its root instead. The walk follows
the persisted lineage (``created_by`` and the crew log's session tree) past closed
sessions, so a worker whose root's tab closed after this process saw the root stays
out of the list too (the known roots live in memory: a root already closed when the
gateway starts is not remembered until it is reopened; the migration part reads it
from saved metadata). A reopened worker keeps its root the same way while its root
is known: its saved metadata line still names its creator, which the stamp reads
for display only (:func:`stamp_rows`). Nothing
here guesses a root from an agent name: a worker that no session created is a
normal session.

A leaf on purpose: it reads slot payload dicts and config objects, never the dashboard
state, so the slot serializer, the session-control handlers and the MCP tools can all
ask it the same question.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from kiro_crew.agent_files import (
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
    TEAM_LEAD_AGENT_FILENAME,
)

#: The agent Crew Mode runs while ``agent.crew_mode_agent`` is unset.
DEFAULT_CREW_MODE_AGENT: Final[str] = CONDUCTOR_AGENT_FILENAME.removesuffix(".json")

#: The shipped conductors: every agent whose job is dispatching work to other
#: sessions, all kept out of a normal chat's agent picker. Derived from the
#: filenames the runtime installs, so a renamed spec cannot leave a stale literal.
CONDUCTOR_TEMPLATES: Final[frozenset[str]] = frozenset(
    name.removesuffix(".json")
    for name in (
        CONDUCTOR_AGENT_FILENAME,
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
        LEDGER_CONDUCTOR_AGENT_FILENAME,
        SECURITY_CONDUCTOR_AGENT_FILENAME,
        TEAM_LEAD_AGENT_FILENAME,
    )
)

#: The slot field (and metadata key) that stamps the agent Crew Mode turned on with.
STAMP_FIELD: Final[str] = "crew_mode_agent"

#: Deprecated names that resolve to the shipped Crew Mode conductor
#: (``conductor_agents.DEPRECATED_AGENT_SPECS``): a chat on one is on that conductor.
#: Derived from the filenames the runtime installs, so a renamed spec cannot leave a
#: stale literal here.
CONDUCTOR_ALIASES: Final[dict[str, str]] = {
    LEDGER_CONDUCTOR_AGENT_FILENAME.removesuffix(".json"): DEFAULT_CREW_MODE_AGENT,
}

#: The row fields the slot list stamps. ``crew_mode_root`` is True on a Crew Mode
#: root; ``crew_root`` is the root's slot key on each of its workers, absent elsewhere.
ROOT_FIELD: Final[str] = "crew_mode_root"
WORKER_FIELD: Final[str] = "crew_root"
#: On a worker row: the session that dispatched it directly, the same creator the
#: root walk starts from (the ``parent`` edge, ``created_by``, the saved creator).
#: Equal to the root for its own crew; a sub-conductor's key for that one's crew.
DISPATCHER_FIELD: Final[str] = "crew_dispatched_by"

#: The last ``agent.crew_mode_agent`` an explicit config read saw (:func:`remember`):
#: the agent catalog and the agent switch pass the config
#: they loaded. It is the fallback only. Every read without a config asks the live
#: config watcher's snapshot first (:func:`_live_cfg`), which the gateway primes from
#: its boot config and refreshes on every edit of the file, so a custom
#: ``agent.crew_mode_agent`` is in force from gateway start without any browser read.
_configured: str = DEFAULT_CREW_MODE_AGENT


def _live_cfg() -> Any:
    """The live config watcher's snapshot, or ``None`` when it is not armed. No I/O."""
    try:
        from kiro_crew.config import live

        return live.snapshot()
    except Exception:
        return None


def _agent_of(cfg: Any) -> str:
    value = getattr(getattr(cfg, "agent", None), "crew_mode_agent", "")
    return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_CREW_MODE_AGENT


#: ``{configured name: conductor in force}`` for a configured ``agent.crew_mode_agent``
#: the migration found unusable (no such agent template, or a crewmate's name). While
#: the name stays configured, every reader gets the conductor last applied instead, so
#: new Crew Mode chats still turn on and the lock still lets the roots stay put.
_unusable: dict[str, str] = {}


def set_unusable(name: str, fallback: str) -> None:
    """Record that the configured *name* cannot run Crew Mode; *fallback* stands in."""
    if name and fallback and name != fallback:
        _unusable.clear()
        _unusable[name] = fallback


def clear_unusable() -> None:
    """Forget any unusable configured name (a later config names a usable one)."""
    _unusable.clear()


def configured_crew_mode_agent(cfg: Any = None) -> str:
    """The name ``agent.crew_mode_agent`` spells, usable or not. No I/O."""
    if cfg is not None:
        remember(cfg)
        return _configured
    live_cfg = _live_cfg()
    if live_cfg is not None:
        return _agent_of(live_cfg)
    return _configured


def crew_mode_agent(cfg: Any = None) -> str:
    """The agent a chat switches to when Crew Mode is turned on.

    From *cfg* when given (and remembered); otherwise from the live config snapshot,
    else the last value remembered. A configured name the migration found unusable
    (:func:`set_unusable`) reads as the conductor last applied. Never reads the
    config file, so the slot serializer can ask it on the event loop.
    """
    name = configured_crew_mode_agent(cfg)
    return _unusable.get(name, name)


def remember(cfg: Any) -> None:
    """Record *cfg*'s ``agent.crew_mode_agent`` for readers that cannot load config."""
    global _configured
    _configured = _agent_of(cfg)


def conductor_agents(cfg: Any = None) -> frozenset[str]:
    """Every conductor: the shipped templates, the configured agent and its aliases.

    The list a normal chat's agent picker leaves out: every conductor is Crew Mode.
    """
    agent = crew_mode_agent(cfg)
    aliases = {alias for alias, to in CONDUCTOR_ALIASES.items() if to == agent}
    return frozenset(CONDUCTOR_TEMPLATES | {agent} | aliases)


def stamp_for_switch(agent: object, agent_kind: object) -> str:
    """The stamp a switch to *agent* records: *agent* when that turns Crew Mode on, else ``""``.

    A switch in the template namespace onto a conductor is Crew Mode turning on. A
    crewmate carrying the name is that crewmate.
    """
    if agent_kind == "member" or not isinstance(agent, str):
        return ""
    return agent if agent in conductor_agents() else ""


def runs_crew_mode(
    agent: object, agent_kind: object, mode: object = "", stamp: object = ""
) -> bool:
    """Whether a session on *agent* (in namespace *agent_kind*) runs Crew Mode.

    Any conductor, or the agent *stamp* names: a chat that turned Crew Mode on with
    a custom conductor absent from ``agent.crew_mode_agent`` stays Crew Mode
    until the migration moves it onto the configured one. The stamp counts only
    while the chat is still on that agent. A crewmate that carries a conductor's
    name is that crewmate, not a conductor.
    """
    if agent_kind == "member" or mode == "member" or not isinstance(agent, str) or not agent:
        return False
    return agent in conductor_agents() or (isinstance(stamp, str) and stamp == agent)


#: Slot keys known to be Crew Mode roots, live or closed. Every root a slot frame
#: stamps is added, as is every key passed to :func:`note_roots`, so a worker whose
#: root's tab closed after this process saw it is still that root's worker. In
#: memory only. A key leaves it only while
#: its slot is LIVE and not a root (:func:`stamp_rows`); a closed key stays,
#: because a root key is never reused for another session.
_known_roots: set[str] = set()


def note_roots(keys: Iterable[str]) -> None:
    """Record *keys* as Crew Mode roots (live or closed)."""
    for key in keys:
        if isinstance(key, str) and key:
            _known_roots.add(key)


def known_roots() -> frozenset[str]:
    """Every Crew Mode root key this process has seen or loaded."""
    return frozenset(_known_roots)


def persisted_parents() -> dict[str, str]:
    """``{slot: creator slot}`` from the crew log's session tree, closed slots included.

    The persisted lineage: the session tree folds every session the crew log saw
    created, so it names a closed worker's creator after its tab is gone. Empty
    when the crew log is off or its tree is not seeded. In memory; never raises.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.enabled():
            return {}
        from kiro_crew.crew_log.session_tree_projection import projection

        tree = projection()
        if not tree.seeded_for_current_store:
            return {}
        nodes = tree.nodes()
    except Exception:
        return {}
    return {
        slot: node.parent_slot
        for slot, node in nodes.items()
        if node.parent_slot and not node.cycle
    }


def persisted_tree_slots() -> frozenset[str] | None:
    """Every slot the crew log's session tree holds a node for, or ``None`` when unread.

    A node with no parent is a session nobody created or one ``session_release``
    let go; a slot with NO node is one the tree never saw (a session from before
    the crew log recorded it). Only the second may fall back to its saved
    ``created_by``. In memory; never raises.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.enabled():
            return None
        from kiro_crew.crew_log.session_tree_projection import projection

        tree = projection()
        if not tree.seeded_for_current_store:
            return None
        return frozenset(tree.nodes())
    except Exception:
        return None


def _row_is_root(row: Mapping[str, Any]) -> bool:
    """Whether *row* (a slot payload) is a Crew Mode root.

    Root means no creator by either reading the sidebar nests on: ``created_by``
    (the session-control create stamp) and the crew log's ``parent`` edge (which also
    carries an adoption). App-owned and remote rows never conduct here.
    """
    if row.get("created_by") or row.get("parent") or row.get("app") or row.get("instance_id"):
        return False
    if (row.get("memory_mode") or "persistent") != "persistent":
        return False
    return runs_crew_mode(
        row.get("agent"), row.get("agent_kind"), row.get("mode"), row.get(STAMP_FIELD)
    )


def lineage_is_settled() -> bool:
    """Whether the slot frame's ``parent`` edges are the gateway's settled lineage.

    True while the crew log records and its projection is seeded for this store
    with no failed seed pending: then a row's ``parent`` (already falling back to a
    just-minted child's ``created_by``) is the whole answer, ``None`` included, so a
    released worker stands on its own. False otherwise, and the birth stamp decides.
    In memory only; never raises.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.enabled():
            return False
        from kiro_crew.crew_log.session_tree_projection import projection

        proj = projection()
        return bool(proj.seeded_for_current_store) and not bool(proj.seed_retry_due)
    except Exception:
        return False


def _creator_of(
    row: Mapping[str, Any],
    settled: bool = False,
    saved: str = "",
    untracked: bool = False,
) -> str:
    """The slot key that holds *row* now, or ``""``.

    The crew log's ``parent`` edge first: it is the current lineage, which an
    adoption (``session_adopt``) moves, so an adopted worker belongs to its new
    root. With *settled* lineage (:func:`lineage_is_settled`) a row whose edge is
    ``None`` has no holder, because ``session_release`` cleared it, unless the
    session tree holds no node for it at all (*untracked*): then *saved*, the
    ``created_by`` its metadata line names, stands in. Otherwise ``created_by``
    (the birth stamp), then *saved*, stand in for a missing edge.
    """
    parent = row.get("parent")
    if isinstance(parent, Mapping):
        cited = parent.get("slot") or parent.get("key")
        if isinstance(cited, str) and cited:
            return cited
    if settled and "parent" in row and not row.get("lineage_pending"):
        return saved if untracked else ""
    created_by = row.get("created_by")
    if isinstance(created_by, str) and created_by:
        return created_by
    return saved


def _live_ancestor_of(row: Mapping[str, Any]) -> str:
    """The nearest OPEN ancestor the slot frame resolved for *row*, or ``""``.

    ``parent.key`` is that ancestor's slot key (``lineage_parents`` moves it past a
    closed creator); ``parent.slot`` names the creator itself, open or not.
    """
    parent = row.get("parent")
    if isinstance(parent, Mapping):
        key = parent.get("key")
        if isinstance(key, str):
            return key
    return ""


def stamp_rows(
    rows: Iterable[dict[str, Any]],
    *,
    settled: bool = False,
    parents: Mapping[str, str] | None = None,
    saved_creators: Mapping[str, str] | None = None,
    tree_slots: Iterable[str] | None = None,
) -> None:
    """Stamp :data:`ROOT_FIELD` on every row and :data:`WORKER_FIELD` on each worker. IN PLACE.

    A worker is found by walking its creators up to a Crew Mode root, so a
    grandchild is a worker of the same root. The walk stops on a repeat, which ends
    it on any input (a cycle in forged records nests nothing). Past a creator that
    is not listed (a closed session) the walk follows the live ancestor the slot
    frame resolved for that row (``parent.key``); failing that, a closed creator
    that is a known Crew Mode root (:func:`known_roots`) holds the row, and any
    other closed creator is followed through the persisted lineage (*parents*,
    default :func:`persisted_parents`, read only when a walk needs it). So a worker
    whose root's tab closed after this process saw the root stays that root's worker
    and out of the person's list, and it is listed under the root again when the
    root is reopened. The known roots are in memory only: after a gateway restart a
    root that is still closed is not remembered, and its workers are plain rows
    until it is reopened (the migration part reads closed roots from saved
    metadata).

    *saved_creators* is each live slot's saved ``created_by`` (the metadata line,
    ``_saved_created_by``), which a reopen does not put back on ``created_by``.
    It places a row only where nothing better speaks: no ``parent`` edge, no
    ``created_by``, and with settled lineage only when the session tree holds no
    node for the row (*tree_slots*, default :func:`persisted_tree_slots`), so a
    release still stands. It is display only: it decides which list shows the
    row and grants no control over it. It is never inferred from the agent.
    """
    rows = list(rows)
    saved_of = saved_creators or {}
    tree: list[frozenset[str] | None] = [None if tree_slots is None else frozenset(tree_slots)]
    tree_read = [tree_slots is not None]

    def untracked(key: str) -> bool:
        if not tree_read[0]:
            tree[0] = persisted_tree_slots()
            tree_read[0] = True
        known = tree[0]
        return known is not None and key not in known and f"dashboard:{key}" not in known

    def creator_of(key: str, row: Mapping[str, Any]) -> str:
        saved = saved_of.get(key, "") or ""
        if not isinstance(saved, str) or saved == key:
            saved = ""
        return _creator_of(row, settled, saved, bool(saved) and settled and untracked(key))

    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("key")
        if isinstance(key, str):
            by_key[key] = row
    # A root has no creator by ANY reading, the saved metadata line included: a
    # reopened child that inherited its conductor's template is still that
    # conductor's worker, not a root of its own.
    roots = {key for key, row in by_key.items() if _row_is_root(row) and not creator_of(key, row)}
    note_roots(roots)
    # A live row that is not a root now (its agent was switched off a conductor
    # while idle, or it was adopted) is no remembered root either: forgetting it
    # here keeps a later close from stamping its sessions back into Crew Mode.
    _known_roots.difference_update(key for key in by_key if key not in roots)
    closed_roots = known_roots() - set(by_key)
    lineage: list[Mapping[str, str] | None] = [parents]

    def persisted(key: str) -> str:
        known = lineage[0]
        if known is None:
            known = lineage[0] = persisted_parents()
        return known.get(key, "") or ""

    for key, row in by_key.items():
        row[ROOT_FIELD] = key in roots
        row.pop(WORKER_FIELD, None)
        row.pop(DISPATCHER_FIELD, None)
        if key in roots:
            continue
        seen = {key}
        current: Mapping[str, Any] | None = row
        creator = creator_of(key, row)
        direct = creator
        while creator and creator not in seen:
            if creator in roots or creator in closed_roots:
                row[WORKER_FIELD] = creator
                row[DISPATCHER_FIELD] = direct
                break
            seen.add(creator)
            above = by_key.get(creator)
            if above is None:
                live_ancestor = _live_ancestor_of(current) if current is not None else ""
                current = None
                creator = live_ancestor or persisted(creator)
                continue
            current = above
            creator = creator_of(creator, above)


def slot_is_crew_mode_root(slot: Any) -> bool:
    """Whether live slot *slot* is a Crew Mode root, by the rule the slot list stamps."""
    return _row_is_root(_live_row(slot))


def in_crew_tree(row: Mapping[str, Any] | None) -> bool:
    """Whether a stamped row is a Crew Mode root or one of its workers."""
    return bool(row) and bool(row.get(ROOT_FIELD) or row.get(WORKER_FIELD))  # type: ignore[union-attr]


def workers_of(rows: Iterable[Mapping[str, Any]], root_key: str) -> list[Mapping[str, Any]]:
    """The stamped rows whose root is *root_key*."""
    return [row for row in rows if root_key and row.get(WORKER_FIELD) == root_key]


#: The ``GET /api/chat/slots?crew=`` value that keeps every row, workers included.
VIEW_ALL: Final[str] = "all"


def list_view(rows: list[dict[str, Any]], view: str, *, agent: bool) -> list[dict[str, Any]]:
    """The session list's Crew Mode view of stamped *rows*.

    A session a Crew Mode root dispatched is that root's to show (its side panel's
    worker list), so a PERSON's list leaves it out unless *view* is ``all``. An
    *agent*'s read keeps every row by default: its folder, status and lineage tools
    resolve their own and their children's rows from this list.

    The dashboard reads ``crew=all`` and leaves workers out of the person's lists
    on the client (the Crew Mode lists part adds that filter), because its live slot
    frame must carry every row; this default serves other person clients (scripts,
    ``curl``).
    """
    if view == VIEW_ALL or agent:
        return rows
    return [r for r in rows if not r.get(WORKER_FIELD)]


def _live_row(slot: Any) -> dict[str, Any]:
    """The fields :func:`stamp_rows` reads, taken off a live slot object."""
    return {
        "key": getattr(slot, "key", ""),
        "created_by": getattr(slot, "_created_by", "") or "",
        "saved_created_by": getattr(slot, "_saved_created_by", "") or "",
        "parent": None,
        "agent": getattr(slot, "agent", ""),
        "agent_kind": getattr(slot, "agent_kind", ""),
        STAMP_FIELD: getattr(slot, STAMP_FIELD, "") or "",
        "mode": getattr(slot, "mode", ""),
        "memory_mode": getattr(slot, "memory_mode", "") or "persistent",
        "app": getattr(slot, "_app", "") or "",
        "instance_id": getattr(slot, "instance_id", "") or "",
    }


def live_stamps(
    slots: Mapping[str, Any], parents: Mapping[str, Any] | None = None, *, settled: bool = False
) -> dict[str, tuple[str, bool]]:
    """``{key: (crew_root, crew_mode_root)}`` for every live slot in *slots*.

    The same rule the full slot frame stamps (:func:`stamp_rows`), applied to the
    live slot objects so a ``slot_patch`` frame can restate the two fields when
    a removal or a lineage change moves them; ``crew_root`` is ``""`` on a row
    that is no worker. *parents* is each key's ``parent`` as the frame computed
    it, so an adoption edge counts as the full frame counts it. No I/O.
    """
    rows = [_live_row(s) for s in list(slots.values())]
    for row in rows:
        if parents is not None:
            row["parent"] = parents.get(row["key"])
        else:
            row.pop("parent", None)
    saved = {str(row["key"]): str(row.pop("saved_created_by") or "") for row in rows}
    stamp_rows(rows, settled=settled and parents is not None, saved_creators=saved)
    return {
        str(row["key"]): (str(row.get(WORKER_FIELD) or ""), bool(row.get(ROOT_FIELD)))
        for row in rows
    }


def slot_in_crew_tree(
    slots: Mapping[str, Any],
    slot_key: str,
    parents: Mapping[str, Any] | None = None,
    *,
    settled: bool = False,
) -> bool:
    """Whether live slot *slot_key* is a Crew Mode root or one of its workers.

    The same rule the slot list stamps (:func:`stamp_rows` then :func:`in_crew_tree`),
    applied to the live slot objects so a server route can ask it without
    serializing the list. Edges come from ``_created_by``, which session-control
    create stamps on every child, and from *parents* (each key's ``parent`` as the
    slot frame resolves it) when given, so an adoption edge and a closed creator's
    live ancestor count as the full frame counts them. No I/O.
    """
    crew_root, is_root = live_stamps(slots, parents, settled=settled).get(slot_key, ("", False))
    return bool(crew_root or is_root)


def slot_in_turned_on_tree(
    slots: Mapping[str, Any],
    slot_key: str,
    parents: Mapping[str, Any] | None = None,
    *,
    settled: bool = False,
) -> bool:
    """Whether live slot *slot_key* belongs to a chat that turned Crew Mode on.

    The folder rule asks this, not :func:`slot_in_crew_tree`: a session leaves the
    person's folders only when its ROOT slot carries the :data:`STAMP_FIELD` stamp,
    which marks a chat that turned Crew Mode on. A root on a conductor template with
    no stamp keeps its folders. True for a stamped root and for a worker whose
    ``crew_root`` is a live, stamped slot; a worker of a closed root is False.
    No I/O.
    """
    crew_root, is_root = live_stamps(slots, parents, settled=settled).get(slot_key, ("", False))
    root_key = slot_key if is_root else crew_root
    if not root_key:
        return False
    root = slots.get(root_key)
    return root is not None and bool(getattr(root, STAMP_FIELD, "") or "")


def row_in_turned_on_tree(
    row: Mapping[str, Any] | None, rows_by_key: Mapping[str, Mapping[str, Any]]
) -> bool:
    """:func:`slot_in_turned_on_tree` for a stamped slot-list row.

    *rows_by_key* is the slot list keyed by ``key``. True for a Crew Mode root row
    with a non-empty :data:`STAMP_FIELD`, and for a worker row whose ``crew_root``
    is a listed row with one; a worker of a closed (unlisted) root is False.
    """
    if not row:
        return False
    if row.get(ROOT_FIELD):
        return bool(row.get(STAMP_FIELD))
    root_key = row.get(WORKER_FIELD)
    if not isinstance(root_key, str) or not root_key:
        return False
    root = rows_by_key.get(root_key)
    return bool(root) and bool(root.get(ROOT_FIELD)) and bool(root.get(STAMP_FIELD))  # type: ignore[union-attr]
