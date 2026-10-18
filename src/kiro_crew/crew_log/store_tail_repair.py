"""Closing an interrupted tail: the deterministic closers a resume appends.

``_close_interrupted_tail`` is ``open(repair=True)``'s work under the lock: ``_open_tail``
folds the log's end into what it left open (``_OpenTail``) -- a turn, tool calls,
approvals, subagents -- and ``_closer_entries`` builds the entries that close each one with
an unknown or interrupted outcome (``STOP_REASON_INTERRUPTED``, ``TOOL_STATUS_UNKNOWN``,
``APPROVAL_DECISION_UNKNOWN``, ``SUBAGENT_OUTCOME_UNKNOWN``). A trailing chunk group no
entry cites is dropped first (``_orphan_chunk_offset``, ``_classify_record``), and a repair
that would have to judge content declines and says why (``_refuse_content_repair``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.crew_log.schema import (
    KIND_SESSION,
    MAX_ENTRY_BYTES,
    Entry,
    require_entry_line,
    serialize,
)
from kiro_crew.crew_log.store_paths import _lock_path, _open_lock, logger
from kiro_crew.crew_log.store_segment_reads import _parses_to_object, _scan_tail
from kiro_crew.crew_log.store_segment_writes import _append_line, _truncate
from kiro_crew.jsonl_util import UnreadableRecord, strict_raw_records


def _refuse_content_repair(unit_id: str, skipped: str) -> None:
    """Say why a content-aware repair is declining to touch this file.

    Named once because both check sites answer the same question -- the fold ran
    over records it could not read, so what it reports about the tail is not what
    the file says -- and a repair that acts on it appends an outcome no writer
    observed, permanently, to a log nothing rewrites.
    """
    logger.warning(
        "session log %r: not repairing an interrupted turn -- the fold skipped a "
        "record (%s), so its result cannot be trusted and appending a closer could "
        "fabricate an outcome",
        unit_id,
        skipped,
    )


def _classify_record(raw: bytes, index: int) -> "tuple[Entry | None, str | None]":
    """*raw* as an entry, or the reason it is not one.

    Both strict walks share this: the scan that produces a truncation offset, and
    the fold that decides whether a closer may be appended. Their results feed
    the same mutation -- one chooses where the file is cut, the other whether
    history is added to it -- so they have to agree about what counts as a
    record. A second spelling of this classification is a second answer waiting
    to disagree with the first.

    *index* is the record's position in the file, so the reason names the line a
    reader can go and look at. Record 1 is the header and is the caller's to skip.
    """
    stripped = raw.strip()
    if not stripped:
        return None, f"record {index + 1} is blank"
    parsed = _parses_to_object(stripped)
    if parsed is None:
        return None, f"record {index + 1} is malformed"
    entry = Entry.from_dict(parsed)
    if entry is None:
        return None, f"record {index + 1} is not a valid entry"
    return entry, None


def _orphan_chunk_offset(path: Path) -> "tuple[int, int, int] | None":
    """A trailing chunk group with no citing entry: its offset and seq range.

    A body too large for one line is written as chunks plus the entry that cites
    their seqs, in one batch. A hard kill during that write can still tear the tail,
    and the citing entry is the LAST line -- so what survives is chunks nothing
    points at. The body is on disk and unreachable: no entry names those seqs, and
    the message the group belongs to has no record at all.

    Dropping them is the honest repair. It costs the one message that was mid-write,
    which is the same residual as any single entry lost to a hard kill, and it leaves
    the file free of lines whose only purpose was to be cited by an entry that does
    not exist.

    Only a TRAILING run counts. A chunk group followed by any other entry was
    completed -- its citing entry landed -- and re-reading which entry cites what is
    not this function's job.

    Returns ``(byte offset of the first orphan, first seq, last seq)`` or ``None``.

    Reads with the ABORT posture, not the skipping one. The offsets computed here
    feed a truncation, and ``bounded_raw_records`` drops an over-cap record in full
    -- terminator included -- without yielding it, so a byte walk over what it does
    yield stops matching the file at the first oversized record and every offset
    after it is short by that record's length. Truncating at a short offset cuts
    valid history, which is the one thing this file's rules forbid. So a record that
    cannot be delivered intact aborts the scan and this returns ``None``: no offset,
    no truncation, the file left exactly as it is with a warning naming why.
    """
    offsets: "list[tuple[int, int, str]]" = []
    try:
        with open(path, "rb") as source:
            at = 0
            for index, raw in enumerate(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES)):
                start = at
                at += len(raw)
                if index == 0:
                    continue
                entry, _ = _classify_record(raw, index)
                if entry is not None:
                    offsets.append((start, entry.seq, entry.type))
    except FileNotFoundError:
        return None
    except UnreadableRecord as exc:
        # Fail closed. Every byte has to be accounted for before an offset into this
        # file can be trusted, and this read could not account for one.
        logger.warning(
            "crew log %r: not scanning for an orphan chunk group -- a record could not "
            "be read intact (%s), so a byte offset into this file cannot be trusted "
            "and truncating on one could cut valid entries",
            path,
            exc,
        )
        return None
    trailing = 0
    while trailing < len(offsets) and offsets[len(offsets) - 1 - trailing][2] == "message/chunk":
        trailing += 1
    if not trailing:
        return None
    first = offsets[len(offsets) - trailing]
    return (first[0], first[1], offsets[-1][1])


#: What a closer records as the reason a turn ended, and as a tool's outcome.
#: Two distinct words on purpose: the turn ENDED (the writer stopped), while the
#: tool's result is simply not knowable from the record.
STOP_REASON_INTERRUPTED = "interrupted"
TOOL_STATUS_UNKNOWN = "unknown"

#: The same "not knowable from the record" word, for the two other openers a
#: session's crew log can be left holding. Only the repair ever writes them: a live
#: writer always knows what a human decided and how a child ended, so a reader
#: seeing either value knows it is reading a reconstruction rather than an
#: observation. Spelled as separate constants because they name different fields
#: on different types, and a future change to one must not silently move the other.
APPROVAL_DECISION_UNKNOWN = "unknown"
SUBAGENT_OUTCOME_UNKNOWN = "unknown"


@dataclass(frozen=True)
class _OpenTail:
    """What a session log was left holding open, in first-seen order.

    ``turn`` is None when the newest turn DID complete and the only thing still
    open is a child that outlived it -- which is the ordinary shape for a subagent,
    since a child routinely finishes turns after the one that spawned it.

    ``calls`` and ``approvals`` are turn-scoped: both belong to the turn that
    opened them and cannot outlive it, so they are collected only from inside an
    open turn. ``children`` is FILE-scoped for the opposite reason -- a child's
    whole point is that it runs past its asking turn, so scoping its closer to the
    open turn would leave exactly the common case unclosed.
    """

    turn: Any
    calls: tuple[dict[str, Any], ...]
    last_time: int
    approvals: tuple[dict[str, Any], ...] = ()
    children: tuple[dict[str, Any], ...] = ()


def _open_tail(
    path: Path, *, child_gone: "Callable[[str], bool] | None" = None
) -> "tuple[_OpenTail | None, str | None]":
    """The unbalanced tail and why a repair fold skipped a record, if it did.

    Three kinds of opener can be left unbalanced, and they are NOT scoped alike.

    A ``turn/started`` with no later ``turn/completed`` means the writer stopped
    mid-turn. Unmatched ``tool/called`` and ``approval/requested`` entries are
    collected only from INSIDE that turn: both are turn-scoped by construction --
    a call completes within its turn, and an approval is decided in the same
    ``finally`` that the turn's own path runs through -- so an unmatched one in a
    turn that DID complete is a different anomaly, and inventing an outcome for it
    here would be this reader editing history it was not asked about.

    An unmatched ``subagent/spawned`` is collected across the WHOLE file instead,
    because a child is deliberately not turn-scoped: it starts, steers and reports
    long after its asking turn ended, so the ordinary dangling case is a child
    whose turn completed normally. Scoping it to the open turn would close only the
    rare child that died inside its own turn and leave every common one open
    forever.

    Children are kept only when *child_gone* says so, and that asymmetry is the
    point. "The writer is gone" is what a repair knows, and for a turn-scoped
    opener that settles it -- the turn died with its writer. It does NOT settle a
    child: a child outlives the turn that asked for it BY DESIGN, so a writer
    torn down inside a live process can leave a child still running, still able to
    file its own real terminal. Closing it from here would put two outcomes for one
    ``agent_id`` in a file nothing rewrites. Nothing in the file distinguishes the
    two, so the question goes to the caller's live subagent registry; with no
    predicate the answer is "still running" and the opener stands.

    This fold feeds a mutation, so it reports every record the ordinary reader
    would skip. A repair cannot distinguish a truly open turn from one whose real
    completion is damaged, and appending a closer after such a skip would make a
    false outcome permanent.
    """
    open_turn: Any = None
    last_time = 0
    last_walked = 0
    calls: dict[str, dict[str, Any]] = {}
    approvals: dict[str, dict[str, Any]] = {}
    children: dict[str, dict[str, Any]] = {}
    skipped: str | None = None
    try:
        with open(path, "rb") as source:
            for index, raw in enumerate(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES)):
                if index == 0:
                    continue
                entry, reason = _classify_record(raw, index)
                if entry is None:
                    skipped = skipped or reason
                    continue
                if entry.seq <= last_walked:
                    # A non-advancing seq is damage the append-only writer cannot
                    # produce (the same verdict ``iter_from`` and ``advance`` give
                    # it). This fold feeds a MUTATION: ``_scan_tail`` takes the
                    # newest line's seq, so a backward tail lowers the closers'
                    # first seq onto records that already exist -- a repair here
                    # would amplify the damage before any reader refuses it.
                    skipped = skipped or (
                        f"entry {entry.seq} at record {index} is at or below the "
                        f"previously read entry's seq {last_walked}"
                    )
                    continue
                last_walked = entry.seq
                last_time = entry.time
                data = entry.data if isinstance(entry.data, dict) else {}
                if entry.type == "turn/started":
                    open_turn = data.get("turn")
                    calls = {}
                    approvals = {}
                elif entry.type == "turn/completed":
                    open_turn = None
                    calls = {}
                    approvals = {}
                elif entry.type == "tool/called" and open_turn is not None:
                    call_id = data.get("call_id")
                    if isinstance(call_id, str) and call_id and call_id not in calls:
                        calls[call_id] = {
                            "call_id": call_id,
                            "name": data.get("name", ""),
                            "server": data.get("server", ""),
                        }
                elif entry.type == "tool/completed" and open_turn is not None:
                    call_id = data.get("call_id")
                    if isinstance(call_id, str):
                        calls.pop(call_id, None)
                elif entry.type == "approval/requested" and open_turn is not None:
                    approval_id = data.get("approval_id")
                    if (
                        isinstance(approval_id, str)
                        and approval_id
                        and approval_id not in approvals
                    ):
                        approvals[approval_id] = {"approval_id": approval_id}
                elif entry.type == "approval/decided" and open_turn is not None:
                    approval_id = data.get("approval_id")
                    if isinstance(approval_id, str):
                        approvals.pop(approval_id, None)
                elif entry.type == "subagent/spawned":
                    agent_id = data.get("agent_id")
                    if isinstance(agent_id, str) and agent_id and agent_id not in children:
                        children[agent_id] = {"agent_id": agent_id}
                elif entry.type in ("subagent/completed", "subagent/failed"):
                    agent_id = data.get("agent_id")
                    if isinstance(agent_id, str):
                        children.pop(agent_id, None)
    except FileNotFoundError:
        return None, None
    except UnreadableRecord as exc:
        skipped = skipped or str(exc)
    # An unmatched opener says the file never recorded an outcome; only the caller
    # can say whether one is still COMING. Without a predicate nothing is closed,
    # and a predicate that raises is read as "still running" -- both leave the
    # opener standing, which is the direction that loses nothing a reader cannot
    # recover. Asking per surviving opener rather than per entry keeps the question
    # to the children that are actually unbalanced.
    if child_gone is None:
        children.clear()
    else:
        for agent_id in list(children):
            try:
                gone = child_gone(agent_id)
            except Exception:
                gone = False
            if not gone:
                children.pop(agent_id, None)
    if open_turn is None and not children:
        return None, skipped
    return (
        _OpenTail(
            turn=open_turn,
            calls=tuple(calls.values()),
            last_time=last_time,
            approvals=tuple(approvals.values()),
            children=tuple(children.values()),
        ),
        skipped,
    )


def _closer_entries(tail: _OpenTail, first_seq: int) -> list[Entry]:
    """The closers for *tail*, in the order they are appended.

    Approvals, then unmatched calls, then children, then the turn. The order is
    the one a LIVE writer could have produced: an approval is decided before the
    call it gates completes, and a turn cannot be closed while a call inside it is
    still open, so closing them the other way round would produce a record no live
    writer could ever have produced.

    The turn closer is written only when a turn was actually open. A child that
    outlived a completed turn leaves this function with children and no turn, and
    appending a ``turn/completed`` there would close a turn that already closed
    itself.

    Every closer reuses the LAST REAL entry's ``time``. A closer describes
    something that happened when the writer stopped, not when a later process
    happened to open the file, so stamping it with the current clock would put a
    gap of arbitrary length inside a turn and make a duration computed off these
    entries a measure of downtime. Reusing the time also makes the repair
    deterministic: the same bytes in produce the same bytes out, whenever it runs.
    """
    entries: list[Entry] = []
    seq = first_seq
    for approval in tail.approvals:
        entries.append(
            Entry(
                type="approval/decided",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={
                    "turn": tail.turn,
                    "approval_id": approval["approval_id"],
                    "decision": APPROVAL_DECISION_UNKNOWN,
                },
            )
        )
        seq += 1
    for call in tail.calls:
        entries.append(
            Entry(
                type="tool/completed",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={
                    "turn": tail.turn,
                    "call_id": call["call_id"],
                    "name": call["name"],
                    "server": call["server"],
                    "status": TOOL_STATUS_UNKNOWN,
                },
            )
        )
        seq += 1
    for child in tail.children:
        # No `turn`: `subagent/failed` carries none, because a child's outcome is
        # not an event of any one turn -- which is the same reason its opener is
        # matched across the whole file rather than inside the open turn.
        entries.append(
            Entry(
                type="subagent/failed",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={
                    "agent_id": child["agent_id"],
                    "outcome": SUBAGENT_OUTCOME_UNKNOWN,
                },
            )
        )
        seq += 1
    if tail.turn is not None:
        entries.append(
            Entry(
                type="turn/completed",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={"turn": tail.turn, "stop_reason": STOP_REASON_INTERRUPTED},
            )
        )
    return entries


def _close_interrupted_tail(
    kind: str,
    unit_id: str,
    path: Path,
    *,
    child_gone: "Callable[[str], bool] | None" = None,
) -> int:
    """Append closers for an interrupted tail. Returns how many were written.

    Session logs only, and RESUME ONLY. A crash, a SIGKILL or a pod eviction
    leaves openers unbalanced, and every later reader then has to carry the same
    special case: is this still running, or did its writer die? Closing the tail
    when the crew log is LOADED after an interruption answers that once, in the
    record, instead of in each reader.

    Two openers are closed on any repair, each with the "not knowable from the
    record" word its own type already uses: an unmatched ``tool/called`` and an
    unmatched ``approval/requested``, both turn-scoped, both settled by the one
    thing a repair knows -- that the writer is gone.

    A third, ``subagent/spawned``, is closed only for an ``agent_id`` the caller's
    *child_gone* predicate reports finished. A child is not turn-scoped, so a
    writer torn down inside a LIVE process can leave a child still running and
    still able to file its own real terminal; closing it from here would put two
    outcomes for one ``agent_id`` in a file nothing rewrites. Only the repairing
    process's own registry of running children can rule that out. This can
    therefore fire on a file whose newest turn completed normally and whose only
    open thing is a child that outlived it.

    "Loaded after an interruption" is the whole precondition, which is why this is
    never reached from a plain ``open``. An open turn is indistinguishable from a
    dead one by looking at the file, so the CALLER's situation is the only thing
    that can tell them apart: a resume knows the previous writer is gone, and a
    live writer reconnecting to its own log knows it is not. Closing a turn
    that is still running would append an outcome it never had and then let the
    turn keep writing past its own completion.

    It stays append-only -- nothing is rewritten, seq continues -- so a reader that
    already folded the file sees only new lines.

    Best-effort by design: a closer that cannot be written leaves the tail open,
    which is the state every reader must already tolerate.
    """
    if kind != KIND_SESSION:
        return 0
    with _open_lock(_lock_path(kind, unit_id)):
        tail = _scan_tail(path)
        if tail.empty:
            return 0
        if tail.torn_offset is not None:
            _truncate(path, tail.torn_offset)
            tail = _scan_tail(path)
        # Check the fold before any content-aware repair. A damaged completion can
        # look exactly like an open turn once the ordinary reader skips it, and a
        # closer appended from that fold would state an outcome no writer observed.
        opened, skipped = _open_tail(path, child_gone=child_gone)
        if skipped is not None:
            _refuse_content_repair(unit_id, skipped)
            return 0
        orphan = _orphan_chunk_offset(path)
        # A chunk group whose citing entry never landed. Dropped BEFORE the closers,
        # so the closers do not sit on top of lines nothing can reach, and before the
        # tail is re-read, so their seqs continue from the truncated file.
        if orphan is not None:
            offset, first_seq, last_seq = orphan
            _truncate(path, offset)
            tail = _scan_tail(path)
            logger.warning(
                "dropped an unreachable chunk group from session log %r: seq %d-%d "
                "were written for a message whose citing entry never landed, so that "
                "one message is missing from this log",
                unit_id,
                first_seq,
                last_seq,
            )
            opened, skipped = _open_tail(path, child_gone=child_gone)
            if skipped is not None:
                _refuse_content_repair(unit_id, skipped)
                return 0
        if opened is None:
            return 0
        needs_newline = tail.needs_newline
        written = 0
        for entry in _closer_entries(opened, tail.last_seq + 1):
            line = require_entry_line(serialize(entry.to_dict()))
            _append_line(path, line, needs_newline=needs_newline)
            needs_newline = False
            written += 1
    logger.info(
        "closed an interrupted tail in session log %r: %d closer(s) appended",
        unit_id,
        written,
    )
    return written
