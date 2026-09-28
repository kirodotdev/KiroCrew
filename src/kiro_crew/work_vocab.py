"""The conductor work board's vocabularies, one spelling for every reader.

Pure data, deliberately outside ``kiro_crew.crew_log``: the ``work/recorded`` entry
type, the work-ledger store (``kiro_crew.work_ledger``) and the tool schemas
(``kiro_crew.validation``) all import these tuples, and the last two sit on the
gateway's boot path while the crew log's storage subsystem must stay unloaded
until an entry point reaches storage. A leaf with no imports of its own is the
only place all three can share without one of them dragging the others in.
"""

from __future__ import annotations

import hashlib
from typing import Any, TypedDict

#: The projection that folds ``work/recorded`` into a board, as its readers name it.
#: Here rather than in ``crew_log``: a reader outside that package needs the name to
#: ask for the fold, and this module is the leaf it can reach without pulling the log's
#: storage subsystem onto the gateway's boot path. Pinned against the kernel's own
#: registry by ``test_pipeline_board_wiring``, because a rename there would otherwise
#: make every such read answer "no board" instead of failing.
WORK_FOLD_NAME: str = "work"

WORK_ACTORS: tuple[str, ...] = ("conductor", "worker")
WORK_ACTIONS: tuple[str, ...] = (
    "create",
    "goal",
    "bind",
    "decide",
    "verdict",
    "close",
    "accept",
    "report",
)
WORK_ITEM_STATES: tuple[str, ...] = ("open", "accepted", "rejected", "abandoned")
WORK_VERDICTS: tuple[str, ...] = ("pass", "fail", "pending", "refused", "error")
WORK_WORKER_STATUSES: tuple[str, ...] = ("progress", "done", "blocked", "question")
WORK_EVENT_KINDS: tuple[str, ...] = ("create", "bind", "report", "decision", "verdict", "close")
#: Items one board may CREATE over its life, open and closed together -- and the
#: number of item records the crew log's ``work`` fold retains per board. ONE value
#: on purpose: the store counts a board's creates in a monotonic counter in its
#: header and the fold counts them in an append-only log, so every create the store
#: admits is one recorded create, a board that can never admit more creates than the
#: fold retains can never overflow the fold, and the fold is then always the whole
#: board rather than a prefix of it. The store enforces it as
#: ``work_ledger.MAX_STORED_ITEMS_PER_CONDUCTOR``; the fold reads it as
#: ``crew_log.projection.WORK_ITEM_LIMIT``. Neither side may import the other
#: (the fold is forbidden from importing the store, and the store must not load
#: the crew log's storage subsystem on the boot path), so the number lives here.
WORK_STORED_ITEM_LIMIT: int = 256
#: Fields a conductor action may set on an item. What the write route logs from
#: the committed item is what the fold applies to the rebuilt one; the two read
#: this one table so they cannot drift apart. A worker's fields are fixed.
WORK_CONDUCTOR_FIELDS: dict[str, tuple[str, ...]] = {
    "create": ("title", "acceptance", "round"),
    "bind": ("worker_session_key",),
    "decide": ("decision", "round"),
    "verdict": ("verdict", "fails"),
    "close": ("state", "decision"),
    "accept": ("acceptance",),
}


# --------------------------------------------------------------------------
# the rendered board's SHAPE, for readers that want it checked
# --------------------------------------------------------------------------
#
# Here for the same reason the tuples above are: every reader of a rendered work
# board shares one spelling of it. ``_work_render`` narrows its return to
# :class:`WorkBoardView`, so a reader that maps a board onto a dashboard's own
# contract type has BOTH ends checked by mypy, and a field renamed here is an error
# at every such reader rather than a key that silently reads as missing. A reader
# that does not want the checking is unaffected -- these are annotations only.


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
    summary: str
    artifacts: dict[str, str]
    pr: int | None
    last_report_at: str | None
    created_at: str
    closed_at: str | None
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
    the count of entries it dropped.

    ``omitted`` is part of the contract rather than an internal detail: an item missing
    from a board is not recoverable by whoever reads the board, so the count has to be
    able to travel to every surface that renders one.
    """

    conductor: WorkBoardConductor
    items: list[WorkBoardItem]
    omitted: int


def work_event_id(ts: str, item_id: str, kind: str, text: str, *, status: str | None = None) -> str:
    """Content-addressed id for one work event line. THE formula, with one home.

    It lives in this leaf because BOTH sides need it and neither may import the other.
    The store writes these ids; the crew-log fold reproduces them for an entry that
    carries none, and the rebuild's completeness checks match events BY id -- so two
    spellings of this formula would let a stored id and a folded id drift apart
    silently, which is the one divergence those checks cannot detect. The fold is
    forbidden from importing the store directly (the work-ledger store has a fixed
    list of permitted importers, so that a board's identity stays server-resolved),
    and a pure content hash is not an identity question, so widening that list for it
    would trade a real boundary for a convenience. A shared leaf costs neither.

    Pipe-separated rather than concatenated, matching Issue Radar's ``_event_id``: bare
    concatenation lets two different tuples produce one string, so two distinct events
    could collapse into each other on read.

    ``status`` IS part of the identity. Timestamps are seconds-precision, so a
    ``progress`` report and a ``blocked`` report with the same summary in the same second
    would otherwise share an id, and first-seen-wins dedupe would drop the transition --
    the one line the conductor most needs to see. ``None`` renders as the empty string,
    so every non-report kind keeps a stable formula.
    """
    raw = f"{ts}|{item_id}|{kind}|{status or ''}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
