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
