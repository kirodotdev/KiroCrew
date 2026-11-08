"""The conductor work board's vocabularies, one spelling for every reader.

Pure data in a leaf with no imports of its own, outside ``kiro_crew.crew_log``: the
``work/recorded`` entry type, the store (``kiro_crew.work_ledger``) and the tool
schemas (``kiro_crew.validation``) all import it, and the last two sit on the
gateway's boot path, where the crew log's storage subsystem must stay unloaded.
"""

from __future__ import annotations

import hashlib
from typing import Any, NotRequired, TypedDict

#: The projection that folds ``work/recorded`` into a board, as its readers name it.
#: Pinned against the kernel's registry by ``test_pipeline_board_wiring``: a rename
#: there would otherwise make every read answer "no board" instead of failing.
WORK_FOLD_NAME: str = "work"

#: ``person`` is the dashboard owner steering a worker from a Crew Mode chat's side
#: panel: it writes one ``steer`` event on the worker's item and sets no field.
WORK_ACTORS: tuple[str, ...] = ("conductor", "worker", "person")
WORK_ACTIONS: tuple[str, ...] = (
    "create",
    "goal",
    "bind",
    "decide",
    "verdict",
    "close",
    "accept",
    "report",
    "steer",
    "retire",
    "destroy",
)
WORK_ITEM_STATES: tuple[str, ...] = ("open", "accepted", "rejected", "abandoned")
WORK_VERDICTS: tuple[str, ...] = ("pass", "fail", "pending", "refused", "error")
#: Every status a worker report may CARRY, so every reader of a stored report (the
#: crew-log entry types, the fold, the metrics series, the boards) knows each one.
#: Which of them a report may be WRITTEN with is :func:`worker_statuses`: ``reply``
#: is admitted only while :data:`WORK_REMOTE_DISPATCH_SETTING` is on.
WORK_WORKER_STATUSES: tuple[str, ...] = ("progress", "done", "blocked", "question", "reply")
#: Worker statuses whose arrival wakes the conductor: ``done`` is a claim to verify,
#: ``blocked`` a dependency to clear, ``question`` a decision only it can make, and
#: ``reply`` an answer to its follow-up that it must relay
#: (rfc-conductor-remote-dispatch §3b). ``progress`` is absent: it reaches the
#: conductor on the next real wake, and waking for it would rebuild polling.
#:
#: Shared here by ``ledger_wake`` (the watch's pure gate) and ``conductor_wake``
#: (on the boot path), which may not import each other; two spellings would let a
#: trigger push news the gate refuses. Both read it through :func:`wake_statuses`,
#: which drops ``reply`` while the preview is off.
WORK_WAKE_STATUSES: tuple[str, ...] = ("done", "blocked", "question", "reply")
#: The statuses only the remote-dispatch Feature Preview admits. With
#: :data:`WORK_REMOTE_DISPATCH_SETTING` off, a report carrying one is refused as an
#: unknown status and no wake reads it, exactly as before the status existed.
WORK_REMOTE_DISPATCH_STATUSES: tuple[str, ...] = ("reply",)
#: The config key of the remote-dispatch Feature Preview (default off), read per call
#: by ``work_preview.remote_dispatch``.
WORK_REMOTE_DISPATCH_SETTING: str = "work_ledger.remote_dispatch"
#: When a ``blocked`` or ``question`` report waits on a person, as data rather than
#: prose: ``approval`` (a tool approval timed out or waits) or ``needs_human`` (only a
#: person can unblock it). A work-ledger watch whose open items all carry one,
#: unchanged since its last turn, holds. A wait on anything else carries no reason.
WORK_BLOCKED_REASONS: tuple[str, ...] = ("approval", "needs_human")
#: The statuses a ``reason`` may ride on.
WORK_REASON_STATUSES: tuple[str, ...] = ("blocked", "question")
#: ``steer`` is a person's own text typed into a worker (actor ``person``). A person's
#: steer outranks the conductor's decision: the conductor reads new steer events
#: before it decides and never overrules them. ``retire`` is the hub moving a remote
#: binding into the item's history (rfc-conductor-remote-dispatch §5b); only the
#: gateway writes it, and only with :data:`WORK_REMOTE_DISPATCH_SETTING` on.
#: ``destroy`` marks a retired binding ``destroyed`` once its peer slot is closed,
#: under the same switch.
WORK_EVENT_KINDS: tuple[str, ...] = (
    "create",
    "bind",
    "report",
    "decision",
    "verdict",
    "close",
    "steer",
    "retire",
    "destroy",
)
#: Item records the ``work`` fold may hold for one board at once: a memory bound on
#: the fold, not on the board's life. The fold holds the open items (at most 32) plus
#: :data:`WORK_CLOSED_WINDOW` closed ones, so a board the store admits never reaches
#: this; it guards a log something else wrote. Also the highest goal ``item_cap``
#: the store accepts. Here because the fold and the store may not import each other.
WORK_STORED_ITEM_LIMIT: int = 256
#: Closed items the ``work`` fold keeps whole, newest-closed last, for the surfaces
#: that draw recent history. Older closed items leave the fold; their full history
#: stays in the crew log.
WORK_CLOSED_WINDOW: int = 32
#: Ids of items the ``work`` fold has forgotten, oldest first, so a late entry for one
#: is dropped instead of parked. Past the cap the oldest CREATED id goes first (a late
#: entry for it then parks and counts in ``omitted``); a baseline-born id goes last,
#: since a late baseline for it would materialise the item again.
WORK_TOMBSTONE_LIMIT: int = 4096

#: The conducting depth cap's default, bounds and config key (meaning documented on
#: ``work_ledger_records.MAX_DEPTH``). Here because the config package needs them
#: and may not import the store.
WORK_MAX_DEPTH_DEFAULT: int = 2
WORK_MAX_DEPTH_MIN: int = 2
WORK_MAX_DEPTH_MAX: int = 10
WORK_MAX_DEPTH_SETTING: str = "work_ledger.max_depth"
#: Fields a conductor action may set on an item. What the write route logs from
#: the committed item is what the fold applies to the rebuilt one; the two read
#: this one table so they cannot drift apart. A worker's fields are fixed.
WORK_CONDUCTOR_FIELDS: dict[str, tuple[str, ...]] = {
    "create": ("title", "acceptance", "round"),
    "bind": ("worker_session_key", "worker_instance_id"),
    "decide": ("decision", "round"),
    "verdict": ("verdict", "fails"),
    "close": ("state", "decision"),
    "accept": ("acceptance",),
    "retire": ("worker_session_key", "worker_instance_id", "retired_bindings"),
    "destroy": ("retired_bindings",),
}


#: Most retired bindings an item keeps; past it the oldest destroyed one goes first.
MAX_RETIRED_BINDINGS = 8

#: The states a retired binding may hold.
RETIRED_BINDING_STATES: frozenset[str] = frozenset({"retired", "destroyed"})

#: The remote pair's caps, one definition for the bind that checks them and the
#: retired history that keeps them (here, since both the store and the fold read
#: them and neither may import the other): a peer slot key, the cap ``bind`` puts
#: on a worker key, and a crew id, the cap the instance registry puts on an id.
WORK_MAX_PEER_SLOT_CHARS: int = 512
WORK_MAX_INSTANCE_ID_CHARS: int = 128
#: An artifact value's cap, so a retired worker's pushed branch is kept whole.
WORK_MAX_ARTIFACT_VALUE_CHARS: int = 512
#: A retire reason and a stamp, as stored on a retired binding.
WORK_MAX_RETIRE_REASON_CHARS: int = 64
WORK_MAX_STAMP_CHARS: int = 64

#: Each retired binding's text fields and their caps.
_RETIRED_BINDING_TEXT: dict[str, int] = {
    "worker_session_key": WORK_MAX_PEER_SLOT_CHARS,
    "worker_instance_id": WORK_MAX_INSTANCE_ID_CHARS,
    "branch": WORK_MAX_ARTIFACT_VALUE_CHARS,
    "reason": WORK_MAX_RETIRE_REASON_CHARS,
    "retired_at": WORK_MAX_STAMP_CHARS,
    "destroyed_at": WORK_MAX_STAMP_CHARS,
}


def coerce_retired_bindings(raw: Any) -> list[dict[str, Any]]:
    """Stored or folded *raw* as a clean retired-binding list. NEVER raises.

    An entry without both halves of the pair, or with an unknown state, is
    dropped; a text field that is not a string reads as ``None``.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw[-MAX_RETIRED_BINDINGS:]:
        state = entry.get("state") if isinstance(entry, dict) else None
        if not isinstance(state, str) or state not in RETIRED_BINDING_STATES:
            continue
        clean: dict[str, Any] = {"state": entry["state"]}
        for name, cap in _RETIRED_BINDING_TEXT.items():
            value = entry.get(name)
            clean[name] = value[:cap] if isinstance(value, str) and value else None
        if clean["worker_session_key"] and clean["worker_instance_id"]:
            out.append(clean)
    return out


def worker_statuses(remote_dispatch: bool) -> tuple[str, ...]:
    """The statuses a ``work_report`` may be written with, in declaration order."""
    if remote_dispatch:
        return WORK_WORKER_STATUSES
    return tuple(s for s in WORK_WORKER_STATUSES if s not in WORK_REMOTE_DISPATCH_STATUSES)


def wake_statuses(remote_dispatch: bool) -> tuple[str, ...]:
    """The worker statuses whose arrival wakes the conductor, in declaration order."""
    if remote_dispatch:
        return WORK_WAKE_STATUSES
    return tuple(s for s in WORK_WAKE_STATUSES if s not in WORK_REMOTE_DISPATCH_STATUSES)


# The rendered board's shape. ``_work_render`` returns :class:`WorkBoardView`, so
# mypy checks both ends of a reader that maps a board onto its own contract type,
# and a renamed field is an error rather than a key that silently reads as missing.


class WorkBoardEvent(TypedDict):
    """One line of an item's history, as rendered (the internal ``_t`` dropped)."""

    id: str
    ts: str
    item_id: str
    kind: str
    #: One of :data:`WORK_WORKER_STATUSES` on a ``report``, else unset.
    status: str | None
    text: str


class WorkBoardItem(TypedDict):
    """One work item: every key the fold starts it with, plus the render's ``schema``."""

    schema: int
    item_id: str
    title: str
    acceptance: dict[str, Any]
    #: One of :data:`WORK_ITEM_STATES`. Closed, so a board's grouping is determined
    #: rather than chosen -- a publisher cannot invent a column name.
    state: str
    #: One of :data:`WORK_VERDICTS`, or unset. An acceptance ruling, NOT a CI result.
    verdict: str | None
    decision: str
    worker_session_key: str | None
    round: int
    #: Acceptance attempts that came back fail. Not a check count either.
    fails: int
    #: One of :data:`WORK_WORKER_STATUSES`, or unset.
    status: str | None
    #: One of :data:`WORK_BLOCKED_REASONS` on a blocked/question report, or unset.
    reason: str | None
    summary: str
    artifacts: dict[str, str]
    pr: int | None
    last_report_at: str | None
    created_at: str
    closed_at: str | None
    #: When the conductor last wrote ``decide`` on the item, or unset.
    decided_at: str | None
    #: When the item's current run of ``pending`` verdicts began, or unset.
    pending_since: str | None
    events: list[WorkBoardEvent]


class WorkBoardConductor(TypedDict):
    """The rendered board's header."""

    schema: int
    slot_key: str
    goal: str
    round: int
    goal_version: int
    depth: int
    parent_item: str | None
    created_at: str
    entries: int
    first_entry_at: str
    #: When the board's NEWEST accepted entry landed. Board metadata like
    #: ``first_entry_at``, and cleared with it when a new generation resets the board.
    last_entry_at: str
    generation: str


class WorkBoardView(TypedDict):
    """What the ``work`` fold renders: the header, every item in creation order, and
    ``omitted``, the count of dropped entries, which every surface must be able to
    show because a reader cannot recover a missing item.
    """

    conductor: WorkBoardConductor
    items: list[WorkBoardItem]
    omitted: int
    #: Closed items the fold has let go, so ``items`` is not the board's whole life.
    #: Optional for a reader: a view without it holds every item.
    forgotten: NotRequired[WorkBoardForgotten]


class WorkBoardForgotten(TypedDict):
    """What the ``work`` fold still knows about the closed items it let go.

    ``created`` counts every item the board has held, forgotten ones included, and
    sets the store's create counter on a rebuild; ``items`` is how many were
    forgotten, split by the state they closed in, which the crew status and the
    pipeline board add to their totals. The store's cache keeps every item's record.
    """

    created: int
    items: int
    accepted: int
    rejected: int
    abandoned: int
    #: Forgotten items created in the board's current round, so "added this round"
    #: still counts them. Zeroed when the round moves.
    in_round: int
    #: Ids of forgotten items a later baseline could name (their tombstones are
    #: protected), so a rebuild counts such a record once. Not a count.
    baseline_ids: NotRequired[list[str]]


def work_event_id(ts: str, item_id: str, kind: str, text: str, *, status: str | None = None) -> str:
    """Content-addressed id for one work event line. The one copy of the formula.

    The store writes these ids and the crew-log fold reproduces them for entries
    without one; the rebuild matches events by id, so two spellings could drift
    undetected. The fold may not import the store (it has a fixed importer list),
    hence this leaf.

    Pipe-separated, as in Issue Radar's ``_event_id``, so two different tuples
    cannot concatenate to one string.

    ``status`` is part of the identity: timestamps are seconds-precision, so a
    ``progress`` and a ``blocked`` report with the same summary in one second would
    otherwise share an id and dedupe would drop the transition. ``None`` renders as
    the empty string.
    """
    raw = f"{ts}|{item_id}|{kind}|{status or ''}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
