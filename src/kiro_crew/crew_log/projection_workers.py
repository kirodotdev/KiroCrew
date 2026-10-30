"""The ``workers`` slot fold: the worker sessions a crewmate created, and what each is doing.

A crewmate hands work to a worker with ``session_create``. Its own crew log records
only the create (``session/spawned``, naming the child's slot); everything the worker
then does -- its turns, what it said, how it ended -- is in the WORKER's own log. So the
value is two folds merged when it is read:

* ``workers`` over the crewmate's OWN units: who it created and when, under the cap;
* ``worker`` over ONE worker's own newest units (:func:`worker_units`): that worker's
  row. A cell per worker, so one worker growing continues its own cell and leaves the
  others warm -- one fold over every worker's log would refold all of them whenever a
  worker that was not the newest wrote.

:func:`merge_render` draws one row per worker from the two. Each reads only entries
the crew log already writes:

* ``session/spawned`` in the crewmate's own unit: a worker exists, and when it began;
* ``session/opened`` in a child unit whose ``parent.slot`` is this crewmate: the
  worker's own log, and its agent;
* ``turn/started`` and ``turn/completed``: running, or between turns, or failed;
* ``message/received`` (the first from the user is the task) and ``message/sent`` (the
  last line of the newest is what it last said);
* ``work/recorded`` with ``actor: worker``: its own report, when it filed one;
* ``session/closed`` and ``session/released``: it stopped, or its creator let it go;
* ``session/adopted`` naming another parent: another session took it over, so it reads
  ``released`` too.

WHAT IT CANNOT KNOW. Whether a worker FINISHED its task is not in the log unless the
worker filed a ``done`` report: a worker that answered and stopped reads ``idle``, the
same as one waiting for its next instruction. The fold says ``idle`` rather than guess
``done``.

An ``Entry`` carries no slot, so whether an entry is the worker's is read from the
unit's own opener (``session/opened`` names its slot), the way ``workstreams`` places a
charge; an entry in a unit whose opener was not seen moves nothing. A worker is drawn
for this crewmate only when its create is in the crewmate's log or its own opener
names the crewmate as its parent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from kiro_crew.crew_log.projection_dashboard import _panel_iso
from kiro_crew.crew_log.projection_values import _as_int, _as_str, _rows_in
from kiro_crew.crew_log.schema import Entry

#: Worker rows one fold keeps, newest first. Every worker past it is counted in
#: ``omitted`` rather than dropped silently.
WORKERS_LIMIT: Final[int] = 12

#: Units read per worker, newest kept: a worker's slot cold-starts a unit per session
#: id, and a page draws what it is doing now.
WORKERS_UNITS_PER_CHILD: Final[int] = 2

#: How much of a task or a line a row keeps.
WORKERS_TEXT_LIMIT: Final[int] = 240

#: The states a row can be in.
STATE_STARTING: Final[str] = "starting"
STATE_RUNNING: Final[str] = "running"
STATE_IDLE: Final[str] = "idle"
STATE_DONE: Final[str] = "done"
STATE_FAILED: Final[str] = "failed"
STATE_BLOCKED: Final[str] = "blocked"
STATE_CLOSED: Final[str] = "closed"
STATE_RELEASED: Final[str] = "released"
WORKER_STATES: Final[tuple[str, ...]] = (
    STATE_STARTING,
    STATE_RUNNING,
    STATE_IDLE,
    STATE_DONE,
    STATE_FAILED,
    STATE_BLOCKED,
    STATE_CLOSED,
    STATE_RELEASED,
)

#: The :func:`kiro_crew.metrics.turns.turn_outcome` labels a turn end can carry that
#: leave a worker between turns rather than failed: a clean end, the person's own
#: Stop, and a watchdog stall that is re-driven in place. Every other outcome --
#: ``timeout``, ``error`` (an interrupt, an unacked cancel) -- reads ``failed``.
_IDLE_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"ok", "cancelled", "tool_stall", "stale_recover"}
)


def _turn_end_state(stop: str) -> str:
    """The state a turn ending with *stop* leaves a worker in, by the shared classifier."""
    from kiro_crew.metrics.turns import turn_outcome

    return STATE_IDLE if turn_outcome(stop) in _IDLE_OUTCOMES else STATE_FAILED


#: The keys of one rendered ``workers.items`` row, for ``dashboard_types``.
WORKER_ROW_KEYS: Final[tuple[str, ...]] = (
    "slot",
    "task",
    "state",
    "agent",
    "started_at",
    "last_activity_at",
    "last_said",
    "report",
    "report_status",
    "turns",
    "stop_reason",
    "closed_reason",
)

#: Every entry type the fold reads.
WORKERS_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session/spawned",
        "session/opened",
        "turn/started",
        "turn/completed",
        "message/received",
        "message/sent",
        "work/recorded",
        "session/closed",
        "session/released",
        "session/adopted",
    }
)


def _clip(value: Any) -> str:
    """*value* as one line of at most :data:`WORKERS_TEXT_LIMIT`, with "…" when cut.

    Read from the raw string, not :func:`_as_str`, whose shorter cut would end the text
    with no "…"; a bounded slice of it, so a huge field costs no more than the line.
    """
    raw = value[: WORKERS_TEXT_LIMIT * 8] if isinstance(value, str) else ""
    text = " ".join(raw.split())
    if len(text) <= WORKERS_TEXT_LIMIT:
        return text
    return text[: WORKERS_TEXT_LIMIT - 1].rstrip() + "…"


#: The provenance line ``session_send`` puts before a message it relays
#: (``session_messaging._SEND_PROVENANCE``): who sent it, not what was asked.
_RELAY_PREFIX: Final[str] = "[sent by session "


#: A line that addresses the worker about its role (``You are a worker on ...``)
#: before the ask itself: a dispatch preamble, not the task.
_PREAMBLE_PREFIX: Final[str] = "you are "
#: The heading a dispatch gives its ask (``TASK: ...``, ``GOAL (issue 3): ...``).
_TASK_LABELS: Final[tuple[str, ...]] = ("task", "goal")


def _labelled_task(text: str) -> str:
    """The ask after a ``TASK:`` or ``GOAL:`` heading at the start of *text*, else ``""``.

    Read with string operations, not a pattern: *text* is a message the fold does not
    bound. A heading may carry a short note before its colon (``TASK (read-only):``).
    """
    head, colon, rest = text.partition(":")
    if not colon or not rest.strip():
        return ""
    label = head.split("(", 1)[0].strip().casefold()
    return rest.strip() if label in _TASK_LABELS and len(head) <= 40 else ""


def _first_line(value: Any) -> str:
    """The task a worker was given: one line, past what is not the ask.

    The relay's own provenance line (who sent it) is skipped. A line headed ``TASK:``
    or ``GOAL:`` is the dispatch naming its ask, so the first such line wins.
    Otherwise the first line with text past a leading role preamble (``You are a
    worker on ...``); a message that is only a preamble keeps it.
    """
    lines = [
        line.strip()
        for line in (value if isinstance(value, str) else "").splitlines()
        if line.strip()
        and not (line.strip().startswith(_RELAY_PREFIX) and line.strip().endswith("]"))
    ]
    for line in lines:
        labelled = _labelled_task(line)
        if labelled:
            return _clip(labelled)
    for line in lines:
        if not line.casefold().startswith(_PREAMBLE_PREFIX):
            return _clip(line)
    return _clip(lines[0]) if lines else ""


def _last_line(value: Any) -> str:
    for line in reversed((value if isinstance(value, str) else "").splitlines()):
        if line.strip():
            return _clip(line)
    return ""


def _workers_start() -> dict[str, Any]:
    return {"slot": "", "rows": {}, "unit_slot": "", "omitted": 0, "dropped": {}}


def _workers_bind_slot(state: dict[str, Any], slot: str) -> None:
    state["slot"] = slot


def _row(state: dict[str, Any], child: str, at: int) -> "dict[str, Any] | None":
    """*child*'s row, made when absent, or ``None`` for a worker the fold let go.

    The fold holds the NEWEST ``WORKERS_LIMIT * 2`` workers by start. The creates are
    folded oldest first, so a full fold makes room for a newer worker by letting the
    oldest go, and refuses one older than every row it holds. Each worker let go is
    counted once in ``omitted``, however many of its entries arrive later.
    """
    rows: dict[str, Any] = state["rows"]
    row = rows.get(child)
    if row is not None:
        return row
    # A dict, not a set: a fold state stays JSON-shaped (its row cost is measured so).
    dropped: dict[str, int] = state.setdefault("dropped", {})
    if child in dropped:
        return None
    if len(rows) >= WORKERS_LIMIT * 2:
        oldest = min(rows.values(), key=lambda r: (_as_int(r["started_ms"]), r["slot"]))
        if (at, child) <= (_as_int(oldest["started_ms"]), oldest["slot"]):
            _drop(state, dropped, child)
            return None
        del rows[oldest["slot"]]
        _drop(state, dropped, oldest["slot"])
    row = _blank_row(child, at)
    rows[child] = row
    return row


def _drop(state: dict[str, Any], dropped: dict[str, int], child: str) -> None:
    """Count *child* in ``omitted`` and remember it, the newest ``WORKERS_LIMIT * 2`` only.

    The memory is bounded like the rows: only a child whose own units are read can
    come back, and :func:`workers_children` names the newest ``WORKERS_LIMIT * 2``
    children, so an older dropped slot is forgotten first.
    """
    state["omitted"] = _as_int(state.get("omitted")) + 1
    dropped[child] = 1
    while len(dropped) > WORKERS_LIMIT * 2:
        del dropped[next(iter(dropped))]


def _workers_step(state: dict[str, Any], entry: Entry) -> None:
    """The crewmate's own creates. Every other type is a worker's, folded by ``worker``."""
    data = entry.data
    own = state.get("slot") or ""
    if entry.type == "session/opened":
        state["unit_slot"] = _as_str(data.get("slot"))
        return
    if entry.type == "session/spawned":
        child = data.get("child")
        child_slot = _as_str(child.get("slot")) if isinstance(child, Mapping) else ""
        if own and state.get("unit_slot") == own and child_slot and child_slot != own:
            _row(state, child_slot, entry.time)


#: Every entry type the ``worker`` fold reads, in the worker's own log.
WORKER_TYPES: Final[frozenset[str]] = WORKERS_TYPES - {"session/spawned"}


def _worker_start() -> dict[str, Any]:
    return {
        "slot": "",
        "unit_slot": "",
        "parent": "",
        "opened": False,
        "asked": False,
        "turn_state": "",
        "row": _blank_row("", 0),
    }


def _worker_bind_slot(state: dict[str, Any], slot: str) -> None:
    state["slot"] = slot
    state["row"]["slot"] = slot


def _blank_row(child: str, at: int) -> dict[str, Any]:
    return {
        "slot": child,
        "task": "",
        "state": STATE_STARTING,
        "agent": "",
        "started_ms": at,
        "last_ms": at,
        "last_said": "",
        "report": "",
        "report_status": "",
        "turns": 0,
        "stop_reason": "",
        "closed_reason": "",
    }


def _worker_step(state: dict[str, Any], entry: Entry) -> None:
    """One worker's own entry, onto its row. Nothing moves it once it was released."""
    if entry.type not in WORKER_TYPES:
        return
    data = entry.data
    own = state.get("slot") or ""
    row: dict[str, Any] = state["row"]
    if entry.type == "session/opened":
        unit_slot = _as_str(data.get("slot"))
        state["unit_slot"] = unit_slot
        if not own or unit_slot != own or row["state"] == STATE_RELEASED:
            return
        parent = data.get("parent")
        parent_slot = _as_str(parent.get("slot")) if isinstance(parent, Mapping) else ""
        if parent_slot and not state.get("parent"):
            # The first opener naming a parent is the creation; a later session of the
            # same slot does not move who made it.
            state["parent"] = _clip(parent_slot)
        if not state.get("opened"):
            state["opened"] = True
            row["started_ms"] = entry.time
        row["agent"] = _clip(data.get("agent"))
        return
    unit_slot = state.get("unit_slot") or ""
    if not own or unit_slot != own or row["state"] == STATE_RELEASED:
        # Not this worker's unit, or let go: what the slot does next is not the work
        # its creator gave it.
        return
    row["last_ms"] = max(_as_int(row["last_ms"]), entry.time)
    if entry.type == "turn/started":
        row["state"] = state["turn_state"] = STATE_RUNNING
        row["turns"] = _as_int(row["turns"]) + 1
    elif entry.type == "turn/completed":
        stop = _as_str(data.get("stop_reason"))
        row["stop_reason"] = stop
        state["turn_state"] = _turn_end_state(stop)
        # A close comes before the end of the turn it cut short, so that end does not
        # reopen the row; nor does it undo the worker's own done or blocked report.
        if row["state"] not in (STATE_DONE, STATE_BLOCKED, STATE_CLOSED):
            row["state"] = state["turn_state"]
    elif entry.type == "message/received":
        if not state.get("asked") and _as_str(data.get("role")) == "user":
            # The FIRST message from the user is the task, even when it was too long
            # for one entry and carries ``chunks`` and no inline ``text``: a later
            # "status?" is not what the worker was given.
            state["asked"] = True
            row["task"] = _first_line(data.get("text"))
    elif entry.type == "message/sent":
        # Always the newest reply: one too long for a single entry carries ``chunks``
        # and no inline ``text``, and an older line kept in its place would read as
        # what the worker said last.
        row["last_said"] = _last_line(data.get("text"))
    elif entry.type == "work/recorded":
        if _as_str(data.get("actor")) != "worker" or _as_str(data.get("by")) != own:
            return
        status = _as_str(data.get("status"))
        if status:
            row["report_status"] = status
            if status == "done":
                row["state"] = STATE_DONE
            elif status in ("blocked", "question"):
                row["state"] = STATE_BLOCKED
            elif row["state"] in (STATE_DONE, STATE_BLOCKED):
                # A later report that is neither (``progress``) takes back the earlier
                # done or blocked: the row is what its turns say again.
                row["state"] = state.get("turn_state") or STATE_RUNNING
        summary = _clip(data.get("summary"))
        if summary:
            row["report"] = summary
    elif entry.type == "session/closed":
        row["state"] = STATE_CLOSED
        row["closed_reason"] = _clip(data.get("reason"))
    elif entry.type == "session/released":
        row["state"] = STATE_RELEASED
    elif entry.type == "session/adopted":
        # Another session took the worker over, with or without a release first: what
        # it does from here is that session's work, not its creator's.
        parent = data.get("parent")
        adopter = _as_str(parent.get("slot")) if isinstance(parent, Mapping) else ""
        if adopter and _clip(adopter) != state.get("parent"):
            row["state"] = STATE_RELEASED


def _worker_render(state: dict[str, Any]) -> dict[str, Any]:
    """One worker's row as :func:`merge_render` reads it, and who opened it as its parent."""
    return {"row": dict(state["row"]), "parent": state.get("parent") or ""}


def _worker_copy(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot": state.get("slot") or "",
        "unit_slot": state.get("unit_slot") or "",
        "parent": state.get("parent") or "",
        "opened": bool(state.get("opened")),
        "asked": bool(state.get("asked")),
        "turn_state": state.get("turn_state") or "",
        "row": dict(state["row"]),
    }


def _worker_rows(state: Mapping[str, Any]) -> int:
    """``worker``: one row."""
    return 1


def merge_render(
    own: dict[str, Any], creator: str, workers: "Mapping[str, Mapping[str, Any]]"
) -> dict[str, Any]:
    """The ``workers`` value: *own* (the creates) merged with each worker's ``worker`` value.

    *workers* maps a child slot to its rendered ``worker`` value. A worker is drawn when
    *own* holds its create, or when its own opener named *creator* as its parent; a
    worker *own* let go past the cap stays counted in ``omitted`` and is not drawn. A
    row keeps the create's start when there is one, so a worker reset since keeps its
    place in the order.
    """
    created: dict[str, Any] = own["rows"]
    dropped = own.get("dropped") or {}
    rows: dict[str, dict[str, Any]] = {}
    for child, spawn in created.items():
        rows[child] = _blank_row(child, _as_int(spawn.get("started_ms")))
    for child, value in workers.items():
        row = value.get("row") if isinstance(value, Mapping) else None
        if not isinstance(row, Mapping) or child == creator or child in dropped:
            continue
        if child not in rows and value.get("parent") != creator:
            continue
        merged = dict(row)
        merged["slot"] = child
        if child in created:
            start = _as_int(created[child].get("started_ms"))
            merged["started_ms"] = start
            merged["last_ms"] = max(_as_int(merged.get("last_ms")), start)
        rows[child] = merged
    return _workers_render({**own, "rows": rows})


def _workers_render(state: dict[str, Any]) -> dict[str, Any]:
    """The workers, newest first, and how many are in each state.

    Each row: ``slot``, ``task`` (the line of what it was asked that names the ask, :func:`_first_line`), ``state``
    (one of :data:`WORKER_STATES`), ``agent``, ``started_at`` and ``last_activity_at``
    (ISO), ``last_said`` (the last line of its newest reply), ``report`` and
    ``report_status`` (its own work report, when it filed one), ``turns``,
    ``stop_reason`` of its last turn and ``closed_reason``. ``counts`` holds every
    state, zero included, over every worker the fold holds, so a page binds
    ``workers.counts.running`` without a script; ``total`` counts every worker and
    ``omitted`` the ones past the :data:`WORKERS_LIMIT` rows kept.
    """
    rows = sorted(
        state["rows"].values(), key=lambda r: (_as_int(r["started_ms"]), r["slot"]), reverse=True
    )
    items = [
        {
            "slot": r["slot"],
            "task": r["task"],
            "state": r["state"],
            "agent": r["agent"],
            "started_at": _panel_iso(_as_int(r["started_ms"])),
            "last_activity_at": _panel_iso(_as_int(r["last_ms"])),
            "last_said": r["last_said"],
            "report": r["report"],
            "report_status": r["report_status"],
            "turns": _as_int(r["turns"]),
            "stop_reason": r["stop_reason"],
            "closed_reason": r["closed_reason"],
        }
        for r in rows[:WORKERS_LIMIT]
    ]
    counts = {name: 0 for name in WORKER_STATES}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return {
        "items": items,
        "counts": counts,
        "total": len(rows) + _as_int(state.get("omitted")),
        "omitted": len(rows) - len(items) + _as_int(state.get("omitted")),
    }


def _workers_copy(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot": state.get("slot") or "",
        "rows": {k: dict(v) for k, v in state["rows"].items()},
        "unit_slot": state.get("unit_slot") or "",
        "omitted": _as_int(state.get("omitted")),
        "dropped": dict(state.get("dropped") or {}),
    }


def _workers_rows(state: Mapping[str, Any]) -> int:
    """``workers``: one row per worker, and one per dropped worker it remembers."""
    return _rows_in(state.get("rows")) + _rows_in(state.get("dropped"))


def workers_units(slot: str) -> "tuple[str, ...]":
    """The units the ``workers`` fold reads for *slot*: the crewmate's own, and no worker's."""
    from kiro_crew.crew_log.store_units import session_units_for_slot

    return tuple(session_units_for_slot(slot))


def workers_children(slot: str) -> "tuple[str, ...]":
    """The workers *slot* created whose ``worker`` cells :func:`merge_render` reads.

    The slots the session tree's CREATION records name *slot* as the creator of
    (:func:`kiro_crew.crew_log.session_tree_projection.created_children`, in memory, no
    I/O), newest first, so a worker *slot* let go is still read and its
    ``session/released`` reaches its row. The same newest ``WORKERS_LIMIT * 2`` the
    fold keeps (:func:`_row`).
    """
    from kiro_crew.crew_log import session_tree_projection

    return session_tree_projection.created_children(slot)[: WORKERS_LIMIT * 2]


def worker_units(child: str, ordered: "Sequence[str]") -> "tuple[str, ...]":
    """The units the ``worker`` fold reads for *child*, out of *ordered*.

    *ordered* is the child's units in ``previous_sid`` succession order, which the
    reader passes (:func:`~kiro_crew.crew_log.projection_readers.units_in_succession`),
    not the header clock's, so a retired session is never folded after its
    replacement; this module sits below the reader and does not import it. A worker
    its creator still holds reads its FIRST unit, which holds the task it was given,
    and its newest :data:`WORKERS_UNITS_PER_CHILD`. A worker
    let go -- the tree's current parent is not its creator, whether it was
    released or released and then adopted -- reads every unit, so the fold reaches the
    ``session/released`` however many sessions came after it, stays ``released``, and
    what the worker did next is not read as its creator's.
    """
    own = tuple(ordered)
    if _let_go(child):
        return own
    return tuple(dict.fromkeys(own[:1] + own[-WORKERS_UNITS_PER_CHILD:]))


def _let_go(child: str) -> bool:
    """Whether *child*'s current parent in the session tree is not its creator. No I/O.

    A slot the tree has no node for yet (only its creator's ``session/spawned`` is
    seen) is still its creator's.
    """
    from kiro_crew.crew_log import session_tree_projection

    if not tree_seeded():
        return False
    try:
        node = session_tree_projection.projection().nodes().get(child)
    except Exception:  # pragma: no cover -- defensive; a read must not fail a fold read
        return False
    if node is None:
        return False
    return node.parent_slot != session_tree_projection.creator_of(child)


def tree_seeded() -> bool:
    """Whether the session tree can answer for the store configured now. No I/O.

    Until it can, it names no workers and no creators, so a ``workers`` value read
    then is provisional: :func:`~kiro_crew.crew_log.projection_readers._read_workers`
    serves it at revision 0, which never outranks the value read once it is seeded.
    """
    from kiro_crew.crew_log import session_tree_projection

    try:
        return bool(session_tree_projection.projection().seeded_for_current_store)
    except Exception:  # pragma: no cover -- defensive; a read must not fail a fold read
        return False
