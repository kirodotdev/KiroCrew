"""Crew Mode: which agents are conductors, which sessions are Crew Mode roots, and
which sessions a root dispatched.

Crew Mode is a chat on a conductor TEMPLATE. ``agent.crew_mode_agent`` names the one a
chat switches to when Crew Mode is turned on, and it is the only place that names it,
so a better conductor is swapped in by changing that one key. The conductor family
(:data:`CONDUCTOR_TEMPLATES` plus the configured agent) is the one server-side list the
dashboard reads to keep conductors out of a normal chat's agent picker: Crew Mode is
the way into a conductor.

A **Crew Mode root** is a ROOT session (nothing dispatched it) running a conductor of
that family as a template, never as a crewmate. Every session it dispatched, directly
or through a session it dispatched, is its **worker**: the slot list stamps each worker
with ``crew_root`` (its root's key) so the person's own session list can leave it out
and the side panel can list it under its root instead.

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
#: sessions. Derived from the filenames the runtime installs, so a renamed spec
#: cannot leave a stale literal here.
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

#: The row fields the slot list stamps. ``crew_mode_root`` is True on a Crew Mode
#: root; ``crew_root`` is the root's slot key on each of its workers, absent elsewhere.
ROOT_FIELD: Final[str] = "crew_mode_root"
WORKER_FIELD: Final[str] = "crew_root"

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


def crew_mode_agent(cfg: Any = None) -> str:
    """The agent a chat switches to when Crew Mode is turned on.

    From *cfg* when given (and remembered); otherwise from the live config snapshot,
    else the last value remembered. Never reads the config file, so the slot
    serializer can ask it on the event loop.
    """
    if cfg is not None:
        remember(cfg)
        return _configured
    live_cfg = _live_cfg()
    if live_cfg is not None:
        return _agent_of(live_cfg)
    return _configured


def remember(cfg: Any) -> None:
    """Record *cfg*'s ``agent.crew_mode_agent`` for readers that cannot load config."""
    global _configured
    _configured = _agent_of(cfg)


def conductor_agents(cfg: Any = None) -> frozenset[str]:
    """The conductor family: the shipped conductors plus the configured Crew Mode agent."""
    return CONDUCTOR_TEMPLATES | {crew_mode_agent(cfg)}


def runs_conductor(agent: object, agent_kind: object, mode: object = "") -> bool:
    """Whether a session on *agent* (in namespace *agent_kind*) runs a conductor template.

    A crewmate that carries a conductor's name is that crewmate, not a conductor.
    """
    if agent_kind == "member" or mode == "member":
        return False
    return isinstance(agent, str) and agent in conductor_agents()


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
    return runs_conductor(row.get("agent"), row.get("agent_kind"), row.get("mode"))


def _creator_of(row: Mapping[str, Any]) -> str:
    """The slot key that dispatched *row*, from either reading, or ``""``."""
    created_by = row.get("created_by")
    if isinstance(created_by, str) and created_by:
        return created_by
    parent = row.get("parent")
    if isinstance(parent, Mapping):
        cited = parent.get("slot") or parent.get("key")
        if isinstance(cited, str):
            return cited
    return ""


def stamp_rows(rows: Iterable[dict[str, Any]]) -> None:
    """Stamp :data:`ROOT_FIELD` on every row and :data:`WORKER_FIELD` on each worker. IN PLACE.

    A worker is found by walking its creators up to a live Crew Mode root, so a
    grandchild is a worker of the same root. The walk stops on a repeat, which ends
    it on any input (a cycle in forged records nests nothing). A creator that is no
    longer listed ends the walk: a worker whose root has closed is shown like any
    other session.
    """
    rows = list(rows)
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("key")
        if isinstance(key, str):
            by_key[key] = row
    roots = {key for key, row in by_key.items() if _row_is_root(row)}
    for key, row in by_key.items():
        row[ROOT_FIELD] = key in roots
        row.pop(WORKER_FIELD, None)
        if key in roots:
            continue
        seen = {key}
        creator = _creator_of(row)
        while creator and creator not in seen:
            if creator in roots:
                row[WORKER_FIELD] = creator
                break
            seen.add(creator)
            above = by_key.get(creator)
            if above is None:
                break
            creator = _creator_of(above)


def in_crew_tree(row: Mapping[str, Any] | None) -> bool:
    """Whether a stamped row is a Crew Mode root or one of its workers."""
    return bool(row) and bool(row.get(ROOT_FIELD) or row.get(WORKER_FIELD))  # type: ignore[union-attr]


def workers_of(rows: Iterable[Mapping[str, Any]], root_key: str) -> list[Mapping[str, Any]]:
    """The stamped rows whose root is *root_key*."""
    return [row for row in rows if root_key and row.get(WORKER_FIELD) == root_key]


#: ``GET /api/chat/slots?crew=`` values. ``roots`` lists only Crew Mode roots (the
#: session filter's Crew Mode chip); ``all`` keeps every row, workers included.
VIEW_ROOTS: Final[str] = "roots"
VIEW_ALL: Final[str] = "all"


def list_view(rows: list[dict[str, Any]], view: str, *, agent: bool) -> list[dict[str, Any]]:
    """The session list's Crew Mode view of stamped *rows*.

    A session a Crew Mode root dispatched is that root's to show (its side panel's
    worker list), so a PERSON's list leaves it out unless *view* is ``all``. An
    *agent*'s read keeps every row by default: its folder, status and lineage tools
    resolve their own and their children's rows from this list. ``roots`` returns
    only Crew Mode roots, for any caller.

    The dashboard reads ``crew=all`` and filters client-side (``crewModeView`` in
    ``website/src/lib/crewMode.ts``), because its live slot frame must carry every
    row; this default serves other person clients (scripts, ``curl``).
    """
    if view == VIEW_ROOTS:
        return [r for r in rows if r.get(ROOT_FIELD)]
    if view == VIEW_ALL or agent:
        return rows
    return [r for r in rows if not r.get(WORKER_FIELD)]


def _live_row(slot: Any) -> dict[str, Any]:
    """The fields :func:`stamp_rows` reads, taken off a live slot object."""
    return {
        "key": getattr(slot, "key", ""),
        "created_by": getattr(slot, "_created_by", "") or "",
        "parent": None,
        "agent": getattr(slot, "agent", ""),
        "agent_kind": getattr(slot, "agent_kind", ""),
        "mode": getattr(slot, "mode", ""),
        "memory_mode": getattr(slot, "memory_mode", "") or "persistent",
        "app": getattr(slot, "_app", "") or "",
        "instance_id": getattr(slot, "instance_id", "") or "",
    }


def slot_in_crew_tree(slots: Mapping[str, Any], slot_key: str) -> bool:
    """Whether live slot *slot_key* is a Crew Mode root or one of its workers.

    The same rule the slot list stamps (:func:`stamp_rows` then :func:`in_crew_tree`),
    applied to the live slot objects so a server route can ask it without
    serializing the list. Edges come from ``_created_by``, which session-control
    create stamps on every child; an adoption edge (crew log only) is not read here.
    No I/O.
    """
    rows = [_live_row(s) for s in list(slots.values())]
    stamp_rows(rows)
    for row in rows:
        if row.get("key") == slot_key:
            return in_crew_tree(row)
    return False
